"""Toplama ve tespit: Prometheus'tan ölçümleri çeker, bulguları üretir."""
from __future__ import annotations

import random
import re
import time
import zlib
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

from .config import Config
from .detect import HourProfile, baseline, detect_fill, detect_jump
from .iforest import Model
from .prom import Prom, PromError
from .signals import CHECKS, GAUGE, GAUGES, MEM_LIMIT_QUERY, Signal, fmt, fmt_duration, label, workload_of

WINDOW = 60  # "normal" hesabında geriye bakılan nokta sayısı (detect_jump ile aynı)
ML_SIGNALS = ("pod_cpu", "pod_mem", "pod_net_rx", "pod_net_tx")  # Isolation Forest'ın birlikte baktığı ölçümler
ML_ROWS = 6000  # iş yükü başına en fazla eğitim satırı


@dataclass
class Finding:
    kind: str       # spike | drop | fill | ml | oom | waiting | restart | pending | unavailable | node_notready | throttle
    severity: int   # 1 bilgi, 2 uyarı, 3 kritik
    scope: str      # pod | node
    namespace: str
    pod: str
    workload: str   # node kapsamında node adı
    title: str
    signal: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        # ML bulgusunun sinyali "en çok sapan ölçüm"dür ve turdan tura değişebilir; anahtara girmez.
        sig = "" if self.kind == "ml" else self.signal
        return f"{self.kind}|{sig}|{self.namespace}|{self.pod or self.workload}"

    @property
    def group(self) -> tuple[str, str]:
        return ("node" if self.scope == "node" else self.namespace, self.workload)


def _r(v: float | None) -> float | None:
    return None if v is None else float(f"{v:.4g}")


class Series:
    """Bir sinyalin bir pod/node için geçmişi. Her noktanın o anki "normal"i ve ölçeği de
    saklanır; ekrandaki eşik zarfı ve sapma haritası tespitle birebir aynı hesabı gösterir."""
    __slots__ = ("ts", "vs", "med", "sc", "seen")

    def __init__(self, n: int):
        self.ts: deque[float] = deque(maxlen=n)
        self.vs: deque[float] = deque(maxlen=n)
        self.med: deque[float | None] = deque(maxlen=n)
        self.sc: deque[float | None] = deque(maxlen=n)
        self.seen = 0.0

    def push(self, t: float, v: float, sig: Signal, cfg: Config) -> None:
        n, p = len(self.vs), cfg.persist
        hist = list(self.vs)[max(0, n + 1 - WINDOW - p):max(0, n + 1 - p)]
        med, sc = baseline(hist, min_abs=sig.min_abs, z=cfg.z) if len(hist) >= cfg.warmup else (None, None)
        self.ts.append(t)
        self.vs.append(v)
        self.med.append(med)
        self.sc.append(sc)
        self.seen = t

    def z(self, i: int) -> float | None:
        med, sc = self.med[i], self.sc[i]
        return None if med is None else (self.vs[i] - med) / sc


class Engine:
    def __init__(self, cfg: Config, prom: Prom):
        self.cfg = cfg
        self.prom = prom
        self.series: dict[tuple[str, str, str], Series] = {}          # (sinyal, namespace, pod/node)
        self.profiles: dict[tuple[str, str, str], HourProfile] = {}   # (sinyal, namespace, iş yükü)
        self.limits: dict[tuple[str, str], float] = {}
        self.profile_at = 0.0
        self.models: dict[tuple[str, str], Model] = {}                # (namespace, iş yükü)
        self.ml_scores: dict[tuple[str, str], deque] = {}             # (namespace, pod) -> (zaman, skor)
        self.ml_missing = 0
        self.ml_train_seconds = 0.0
        self.suppressed = 0
        self.errors: list[str] = []
        self.stats: dict = {}
        self._skip = re.compile(cfg.ns_exclude) if cfg.ns_exclude else None

    # --- toplama -----------------------------------------------------------

    def _ident(self, sig, labels: dict) -> tuple[str, str] | None:
        if sig.scope == "node":
            return "", labels.get("instance") or labels.get("node") or "node"
        ns, pod = label(labels, "namespace"), label(labels, "pod")
        if not pod or (self._skip and self._skip.search(ns)):
            return None
        return ns, pod

    def _query(self, fn, *args) -> list:
        started = time.time()
        try:
            return fn(*args)
        except PromError as e:
            self.errors.append(str(e))
            return []
        finally:
            self.stats["queries"] = self.stats.get("queries", 0) + 1
            self.stats["query_ms"] = self.stats.get("query_ms", 0) + (time.time() - started) * 1000

    def _push(self, sig: Signal, ident: tuple[str, str], t: float, v: float) -> None:
        s = self.series.get((sig.name, *ident))
        if s is None:
            s = self.series[(sig.name, *ident)] = Series(self.cfg.history_points)
        s.push(t, v, sig, self.cfg)

    def backfill(self, now: float) -> None:
        """Açılışta tamponları Prometheus geçmişiyle doldurur; ısınma beklemeden tespit başlar."""
        step = self.cfg.interval
        for sig in GAUGES:
            for labels, pts in self._query(self.prom.range, sig.query, now - self.cfg.history_points * step, now, step):
                ident = self._ident(sig, labels)
                if ident is not None:
                    for t, v in pts:
                        self._push(sig, ident, t, v)

    def refresh_profiles(self, now: float) -> None:
        """Son 7 günden saatlik profilleri kurar ve aynı veriyle Isolation Forest modellerini yeniden eğitir."""
        profiles: dict[tuple[str, str, str], HourProfile] = {}
        rows: dict[tuple[str, str], dict[float, list[float]]] = defaultdict(dict)  # (ns, pod) -> zaman -> ölçümler
        for sig in GAUGES:
            col = ML_SIGNALS.index(sig.name) if sig.name in ML_SIGNALS else None
            for labels, pts in self._query(self.prom.range, sig.query, now - self.cfg.profile_days * 86400, now, 300):
                ident = self._ident(sig, labels)
                if ident is None:
                    continue
                ns, name = ident
                prof = profiles.setdefault((sig.name, ns, workload_of(name) if sig.scope == "pod" else name), HourProfile())
                at = rows[ident] if col is not None else None
                for t, v in pts:
                    prof.add(t, v)
                    if at is not None:
                        at.setdefault(t, [None] * len(ML_SIGNALS))[col] = v
        if profiles:
            self.profiles = profiles
        if self.cfg.ml and rows:
            self.train(rows, now)
        self.profile_at = now

    def train(self, rows: dict[tuple[str, str], dict[float, list]], now: float) -> None:
        """İş yükü başına bir orman. Etiket yok: model "bu iş yükü normalde nasıl görünür"ü verinin kendisinden öğrenir."""
        started = time.time()
        cfg = self.cfg
        units = [GAUGE[n].min_abs for n in ML_SIGNALS]
        by_wl: dict[tuple[str, str], list[list[float]]] = defaultdict(list)
        pods: Counter = Counter()
        for (ns, pod), at in rows.items():
            wl = workload_of(pod)
            pods[(ns, wl)] += 1
            # CPU ve bellek şart; ağ ölçümü olmayan pod'larda (ör. hostNetwork) ağ 0 sayılır, skorlarken de öyle.
            by_wl[(ns, wl)] += [[v or 0.0 for v in r] for r in at.values() if r[0] is not None and r[1] is not None]
        models = {}
        for key, data in by_wl.items():
            if len(data) < cfg.ml_min_samples:
                continue
            seed = zlib.crc32(f"{key[0]}/{key[1]}".encode())
            if len(data) > ML_ROWS:
                data = random.Random(seed).sample(data, ML_ROWS)
            models[key] = Model(data, units, trees=cfg.ml_trees, sample=cfg.ml_sample, seed=seed,
                                q=cfg.ml_quantile, trained_at=now, pods=pods[key])
        self.models = models
        self.ml_train_seconds = time.time() - started

    def collect(self, now: float) -> None:
        self.errors = []
        self.stats = {"queries": 0, "query_ms": 0.0}
        # Henüz modeli olmayan iş yükü varsa (yeni kurulum, yeni uygulama) saatte bir yeniden denenir.
        if (now - self.profile_at >= self.cfg.profile_refresh
                or (self.cfg.ml and self.ml_missing and now - self.profile_at >= 3600)):
            self.refresh_profiles(now)
        for sig in GAUGES:
            for labels, v in self._query(self.prom.instant, sig.query, now):
                ident = self._ident(sig, labels)
                if ident is not None:
                    self._push(sig, ident, now, v)
        stale = now - 10 * self.cfg.interval
        for key in [k for k, s in self.series.items() if s.seen < stale]:
            del self.series[key]
        for key in [k for k in self.ml_scores if ("pod_cpu", *k) not in self.series]:
            del self.ml_scores[key]
        limits = self._query(self.prom.instant, MEM_LIMIT_QUERY, now)
        if limits:
            self.limits = {(label(l, "namespace"), label(l, "pod")): v for l, v in limits}

    # --- tespit ------------------------------------------------------------

    def detect(self, now: float) -> list[Finding]:
        cfg = self.cfg
        out: list[Finding] = []
        candidates = suppressed = fills = 0
        for (name, ns, ent), s in self.series.items():
            if s.seen != now:
                continue
            sig = GAUGE[name]
            vs = list(s.vs)
            pod = ent if sig.scope == "pod" else ""
            wl = workload_of(ent) if sig.scope == "pod" else ent
            jump = detect_jump(vs, min_abs=sig.min_abs, z=cfg.z, persist=cfg.persist, warmup=cfg.warmup, window=WINDOW)
            if jump:
                candidates += 1
                prof = self.profiles.get((name, ns, wl))
                if prof and prof.expected(jump, now):
                    suppressed += 1
                else:
                    up = jump.direction == "up"
                    title = f"{sig.title} aniden {'arttı' if up else 'düştü'}: {fmt(jump.baseline, sig.unit)} → {fmt(jump.value, sig.unit)}"
                    if up and jump.baseline > 0 and jump.value / jump.baseline >= 1.5:
                        title += f" ({jump.value / jump.baseline:.1f} kat)"
                    out.append(Finding("spike" if up else "drop", 2, sig.scope, ns, pod, wl, title, name,
                                       {"value": jump.value, "baseline": jump.baseline, "score": round(jump.score, 1)}))
            if name == "pod_mem":
                limit = self.limits.get((ns, ent))
                eta = detect_fill(list(s.ts), vs, limit, horizon=cfg.fill_horizon) if limit else None
                if eta is not None:
                    fills += 1
                    title = (f"bellek limite gidiyor: şu an {fmt(vs[-1], 'bytes')} / {fmt(limit, 'bytes')} "
                             f"(%{100 * vs[-1] / limit:.0f}), bu hızla yaklaşık {fmt_duration(eta)} sonra dolar")
                    out.append(Finding("fill", 3 if eta < 3600 else 2, "pod", ns, pod, wl, title, name,
                                       {"value": vs[-1], "limit": limit, "eta": round(eta)}))
        self.suppressed += suppressed
        ml = self._ml(now)
        checks = self._checks(now)
        self.stats |= {"candidates": candidates, "suppressed": suppressed, "fills": fills,
                       "ml_scored": self.stats.get("ml_scored", 0), "ml_flags": len(ml),
                       "checks": dict(Counter(f.kind for f in checks)), "findings": len(out) + len(ml) + len(checks)}
        return out + ml + checks

    def ml_vector(self, ns: str, pod: str, now: float) -> list[float] | None:
        """Pod'un bu turdaki ölçümleri, modelin beklediği sırayla."""
        x = []
        for name in ML_SIGNALS:
            s = self.series.get((name, ns, pod))
            ok = s is not None and s.seen == now
            if not ok and name in ("pod_cpu", "pod_mem"):
                return None
            x.append(s.vs[-1] if ok else 0.0)
        return x

    def _ml(self, now: float) -> list[Finding]:
        cfg = self.cfg
        out: list[Finding] = []
        scored = missing = 0
        if not cfg.ml:
            return out
        for (name, ns, pod), s in self.series.items():
            if name != "pod_cpu" or s.seen != now:
                continue
            wl = workload_of(pod)
            model = self.models.get((ns, wl))
            x = self.ml_vector(ns, pod, now)
            if model is None or x is None:
                missing += model is None
                continue
            score = model.score(x)
            scored += 1
            hist = self.ml_scores.get((ns, pod))
            if hist is None:
                hist = self.ml_scores[(ns, pod)] = deque(maxlen=cfg.history_points)
            hist.append((now, score))
            recent = list(hist)[-cfg.persist:]
            if len(recent) < cfg.persist or any(sc < model.threshold for _, sc in recent):
                continue
            if now - recent[0][0] > (cfg.persist - 1) * cfg.interval * 1.5:  # arada boşluk varsa ardışık sayılmaz
                continue
            devs = model.deviations(x)
            order = sorted(range(len(ML_SIGNALS)), key=lambda j: -abs(devs[j]))
            parts = []
            for j in order[:2]:
                if abs(devs[j]) >= 0.5:
                    u = GAUGE[ML_SIGNALS[j]].unit
                    parts.append(f"{GAUGE[ML_SIGNALS[j]].title} {fmt(x[j], u)} (normalde {fmt(model.lo[j], u)}–{fmt(model.hi[j], u)})")
            head = f"ML: ölçümlerin birleşimi olağandışı (skor {score:.2f}, eşik {model.threshold:.2f})"
            title = (f"{head}; {', '.join(parts)}" if parts else
                     f"{head}; tek tek bakınca hepsi olağan aralıkta, birlikte daha önce görülmedi")
            out.append(Finding("ml", 2 if score >= model.threshold + 0.1 else 1, "pod", ns, pod, wl, title,
                               ML_SIGNALS[order[0]],
                               {"score": round(score, 3), "threshold": round(model.threshold, 3),
                                "dev": {ML_SIGNALS[j]: round(devs[j], 1) for j in range(len(ML_SIGNALS))},
                                "values": {ML_SIGNALS[j]: _r(x[j]) for j in range(len(ML_SIGNALS))},
                                "normal": {ML_SIGNALS[j]: [_r(model.lo[j]), _r(model.hi[j])] for j in range(len(ML_SIGNALS))}}))
        self.ml_missing = missing
        self.stats["ml_scored"] = scored
        return out

    def ml_view(self) -> dict:
        """Ekran için: modellerin özeti ve pod'ların son skorları."""
        pods = []
        for (ns, pod), hist in self.ml_scores.items():
            m = self.models.get((ns, workload_of(pod)))
            if m and hist:
                pods.append({"ns": ns, "pod": pod, "workload": workload_of(pod), "score": round(hist[-1][1], 3),
                             "threshold": round(m.threshold, 3)})
        pods.sort(key=lambda r: -(r["score"] - r["threshold"]))
        return {"enabled": self.cfg.ml, "signals": list(ML_SIGNALS), "trees": self.cfg.ml_trees, "sample": self.cfg.ml_sample,
                "min_samples": self.cfg.ml_min_samples, "quantile": self.cfg.ml_quantile,
                "models": len(self.models), "missing": self.ml_missing, "train_seconds": round(self.ml_train_seconds, 2),
                "trained_at": max((m.trained_at for m in self.models.values()), default=None),
                "next_try": self.profile_at + (3600 if self.ml_missing else self.cfg.profile_refresh), "pods": pods}

    def ml_detail(self, ns: str, wl: str) -> dict | None:
        """Bir iş yükünün modeli: eğitim verisinden örnek noktalar, eşik, pod'ların skor geçmişi ve şimdiki ölçümleri."""
        m = self.models.get((ns, wl))
        if m is None:
            return None
        pods = []
        for (pns, pod), hist in self.ml_scores.items():
            if pns != ns or workload_of(pod) != wl or not hist:
                continue
            x = [self.last(n, ns, pod) or 0.0 for n in ML_SIGNALS]
            pods.append({"pod": pod, "ts": [round(t) for t, _ in hist], "scores": [round(v, 3) for _, v in hist],
                         "x": [_r(v) for v in x], "dev": [round(d, 1) for d in m.deviations(x)]})
        return {"ns": ns, "workload": wl, "signals": list(ML_SIGNALS), "units": m.units, "threshold": round(m.threshold, 3),
                "samples": m.samples, "pods_seen": m.pods, "trained_at": m.trained_at, "trees": len(m.forest.trees),
                "psi": m.forest.psi, "median": [_r(v) for v in m.median],
                "lo": [_r(v) for v in m.lo], "hi": [_r(v) for v in m.hi],
                "preview": m.preview, "pods": pods}

    def _checks(self, now: float) -> list[Finding]:
        out: list[Finding] = []
        explained: set[tuple[str, str]] = set()  # oom/waiting varsa aynı pod için "restart" tekrar etmesin
        for chk in CHECKS:
            for labels, v in self._query(self.prom.instant, chk.query, now):
                ns = label(labels, "namespace")
                if self._skip and self._skip.search(ns):
                    continue
                pod, c = label(labels, "pod"), label(labels, "container")
                if chk.scope == "node":
                    out.append(Finding(chk.kind, chk.severity, "node", "", "", labels.get("node", "node"), "node Ready durumda değil"))
                    continue
                if chk.scope == "deploy":
                    out.append(Finding(chk.kind, chk.severity, "pod", ns, "", labels.get("deployment", ""),
                                       f"Deployment'ta {v:.0f} kopya 10 dk'dır hazır değil"))
                    continue
                if chk.kind == "restart" and (ns, pod) in explained:
                    continue
                title = {
                    "oom": f"{c} konteyneri bellek limitini aştığı için öldürüldü (OOMKilled)",
                    "waiting": f"{c} konteyneri başlatılamıyor: {labels.get('reason', '')}",
                    "restart": f"{c} konteyneri son 10 dk'da {max(1, round(v))} kez yeniden başladı",
                    "pending": "pod 10 dk'dır Pending durumda, bir node'a yerleşemiyor",
                    "throttle": f"CPU limiti yüzünden kısılıyor (periyotların %{v * 100:.0f}'i)",
                }[chk.kind]
                if chk.kind in ("oom", "waiting"):
                    explained.add((ns, pod))
                out.append(Finding(chk.kind, chk.severity, "pod", ns, pod, workload_of(pod), title,
                                   detail={"container": c} if c else {}))
        return out

    # --- ekran için --------------------------------------------------------

    def chart(self, signal: str, namespace: str, entity: str, now: float) -> dict | None:
        """Bir serinin ekranda çizilecek hâli: değerler, her noktanın normali ve ölçeği, profil, limit."""
        s = self.series.get((signal, namespace, entity))
        if s is None or not s.vs:
            return None
        sig = GAUGE[signal]
        wl = workload_of(entity) if sig.scope == "pod" else entity
        prof = self.profiles.get((signal, namespace, wl))
        return {
            "signal": signal, "title": sig.title, "unit": sig.unit, "entity": entity, "namespace": namespace,
            "ts": [round(t) for t in s.ts], "vs": [_r(v) for v in s.vs],
            "med": [_r(v) for v in s.med], "sc": [_r(v) for v in s.sc],
            "band": prof.band(now) if prof else None,
            "limit": self.limits.get((namespace, entity)) if signal == "pod_mem" else None,
        }

    def heat(self, signal: str, cols: int = 60) -> dict:
        """Sapma haritası: her pod için son `cols` noktanın işaretli z-skoru (artış +, düşüş −)."""
        rows = []
        for (name, ns, ent), s in self.series.items():
            if name != signal:
                continue
            n = len(s.vs)
            idx = range(max(0, n - cols), n)
            zs = [s.z(i) for i in idx]
            pad = [None] * (cols - len(zs))
            recent = [abs(z) for z in zs[-15:] if z is not None]
            rows.append({"ns": ns, "pod": ent, "workload": workload_of(ent), "peak": round(max(recent, default=0), 1),
                         "z": pad + [None if z is None else round(z, 1) for z in zs],
                         "v": pad + [_r(s.vs[i]) for i in idx], "med": pad + [_r(s.med[i]) for i in idx]})
        rows.sort(key=lambda r: (-r["peak"], r["ns"], r["pod"]))
        longest = max((s for (name, _, _), s in self.series.items() if name == signal), key=lambda s: len(s.ts), default=None)
        ts = [round(t) for t in list(longest.ts)[-cols:]] if longest else []
        return {"ts": [None] * (cols - len(ts)) + ts, "rows": rows[:150], "hidden": max(0, len(rows) - 150)}

    def top(self, signal: str, n: int = 8) -> list[tuple[str, str, float]]:
        rows = [(ns, ent, s.vs[-1]) for (name, ns, ent), s in self.series.items() if name == signal and s.vs]
        return sorted(rows, key=lambda r: -r[2])[:n]

    def last(self, signal: str, namespace: str, entity: str) -> float | None:
        s = self.series.get((signal, namespace, entity))
        return s.vs[-1] if s and s.vs else None

    def last_z(self, signal: str, namespace: str, entity: str) -> float | None:
        s = self.series.get((signal, namespace, entity))
        return s.z(len(s.vs) - 1) if s and s.vs else None
