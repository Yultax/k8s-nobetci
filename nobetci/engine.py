"""Toplama ve tespit: Prometheus'tan ölçümleri çeker, bulguları üretir."""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from .config import Config
from .detect import HourProfile, detect_fill, detect_jump
from .prom import Prom, PromError
from .signals import CHECKS, GAUGE, GAUGES, MEM_LIMIT_QUERY, fmt, fmt_duration, label, workload_of


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


class Series:
    __slots__ = ("ts", "vs", "seen")

    def __init__(self, n: int):
        self.ts: deque[float] = deque(maxlen=n)
        self.vs: deque[float] = deque(maxlen=n)
        self.seen = 0.0


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
        try:
            return fn(*args)
        except PromError as e:
            self.errors.append(str(e))
            return []

    def backfill(self, now: float) -> None:
        """Açılışta tamponları Prometheus geçmişiyle doldurur; ısınma beklemeden tespit başlar."""
        step = self.cfg.interval
        for sig in GAUGES:
            for labels, pts in self._query(self.prom.range, sig.query, now - self.cfg.history_points * step, now, step):
                ident = self._ident(sig, labels)
                if ident is None or not pts:
                    continue
                s = self.series.setdefault((sig.name, *ident), Series(self.cfg.history_points))
                for t, v in pts:
                    s.ts.append(t)
                    s.vs.append(v)
                s.seen = pts[-1][0]

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
        if now - self.profile_at >= self.cfg.profile_refresh:
            self.refresh_profiles(now)
        for sig in GAUGES:
            for labels, v in self._query(self.prom.instant, sig.query, now):
                ident = self._ident(sig, labels)
                if ident is None:
                    continue
                s = self.series.get((sig.name, *ident))
                if s is None:
                    s = self.series[(sig.name, *ident)] = Series(self.cfg.history_points)
                s.ts.append(now)
                s.vs.append(v)
                s.seen = now
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
        for (name, ns, ent), s in self.series.items():
            if s.seen != now:
                continue
            sig = GAUGE[name]
            vs = list(s.vs)
            pod = ent if sig.scope == "pod" else ""
            wl = workload_of(ent) if sig.scope == "pod" else ent
            jump = detect_jump(vs, min_abs=sig.min_abs, z=cfg.z, persist=cfg.persist, warmup=cfg.warmup)
            if jump:
                prof = self.profiles.get((name, ns, wl))
                if prof and prof.expected(jump, now):
                    self.suppressed += 1
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
                    title = (f"bellek limite gidiyor: şu an {fmt(vs[-1], 'bytes')} / {fmt(limit, 'bytes')} "
                             f"(%{100 * vs[-1] / limit:.0f}), bu hızla yaklaşık {fmt_duration(eta)} sonra dolar")
                    out.append(Finding("fill", 3 if eta < 3600 else 2, "pod", ns, pod, wl, title, name,
                                       {"value": vs[-1], "limit": limit, "eta": round(eta)}))
        out.extend(self._checks(now))
        return out

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
        s = self.series.get((signal, namespace, entity))
        if s is None or not s.vs:
            return None
        sig = GAUGE[signal]
        wl = workload_of(entity) if sig.scope == "pod" else entity
        prof = self.profiles.get((signal, namespace, wl))
        return {
            "signal": signal, "title": sig.title, "unit": sig.unit, "entity": entity,
            "ts": [round(t) for t in s.ts], "vs": list(s.vs),
            "band": prof.band(now) if prof else None,
            "limit": self.limits.get((namespace, entity)) if signal == "pod_mem" else None,
        }

    def top(self, signal: str, n: int = 8) -> list[tuple[str, str, float]]:
        rows = [(ns, ent, s.vs[-1]) for (name, ns, ent), s in self.series.items() if name == signal and s.vs]
        return sorted(rows, key=lambda r: -r[2])[:n]

    def last(self, signal: str, namespace: str, entity: str) -> float | None:
        s = self.series.get((signal, namespace, entity))
        return s.vs[-1] if s and s.vs else None
