"""Toplama ve tespit: Prometheus'tan ölçümleri çeker, bulguları üretir."""
from __future__ import annotations

import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field

from .config import Config
from .detect import HourProfile, baseline, detect_fill, detect_jump
from .prom import Prom, PromError
from .signals import CHECKS, GAUGE, GAUGES, MEM_LIMIT_QUERY, Signal, fmt, fmt_duration, label, workload_of

WINDOW = 60  # "normal" hesabında geriye bakılan nokta sayısı (detect_jump ile aynı)


@dataclass
class Finding:
    kind: str       # spike | drop | fill | oom | waiting | restart | pending | unavailable | node_notready | throttle
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
        return f"{self.kind}|{self.signal}|{self.namespace}|{self.pod or self.workload}"

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
        profiles: dict[tuple[str, str, str], HourProfile] = {}
        for sig in GAUGES:
            for labels, pts in self._query(self.prom.range, sig.query, now - self.cfg.profile_days * 86400, now, 300):
                ident = self._ident(sig, labels)
                if ident is None:
                    continue
                ns, name = ident
                prof = profiles.setdefault((sig.name, ns, workload_of(name) if sig.scope == "pod" else name), HourProfile())
                for t, v in pts:
                    prof.add(t, v)
        if profiles:
            self.profiles = profiles
        self.profile_at = now

    def collect(self, now: float) -> None:
        self.errors = []
        self.stats = {"queries": 0, "query_ms": 0.0}
        if now - self.profile_at >= self.cfg.profile_refresh:
            self.refresh_profiles(now)
        for sig in GAUGES:
            for labels, v in self._query(self.prom.instant, sig.query, now):
                ident = self._ident(sig, labels)
                if ident is not None:
                    self._push(sig, ident, now, v)
        stale = now - 10 * self.cfg.interval
        for key in [k for k, s in self.series.items() if s.seen < stale]:
            del self.series[key]
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
        checks = self._checks(now)
        self.stats |= {"candidates": candidates, "suppressed": suppressed, "fills": fills,
                       "checks": dict(Counter(f.kind for f in checks)), "findings": len(out) + len(checks)}
        return out + checks

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
