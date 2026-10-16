"""Bulguları olaylara toplar: aynı iş yüküne (ya da node'a) ait bulgular tek olay olur."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

from . import rules
from .engine import Engine, Finding


@dataclass
class Incident:
    id: int
    scope: str       # pod | node
    namespace: str
    workload: str
    opened: float
    last_seen: float
    closed: float | None = None
    severity: int = 1
    findings: dict[str, dict] = field(default_factory=dict)
    charts: dict[str, dict] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    llm_sig: str = ""     # LLM'in en son yorumladığı durumun imzası
    llm_try: float = 0.0
    feedback: str = ""   # kullanıcının işareti: "dogru" | "yanlis" | ""

    @property
    def group(self) -> tuple[str, str]:
        return ("node" if self.scope == "node" else self.namespace, self.workload)

    @property
    def entity(self) -> str:
        return self.workload if self.scope == "node" else f"{self.namespace}/{self.workload}"

    @property
    def title(self) -> str:
        more = len(self.findings) - 1
        return f"{self.entity}: {rules.top_finding(self.findings)['title']}" + (f" (+{more} bulgu)" if more else "")

    @property
    def signature(self) -> str:
        return f"{self.severity}:" + ",".join(sorted({f["kind"] for f in self.findings.values()}))

    def to_dict(self) -> dict:
        return asdict(self) | {"title": self.title, "entity": self.entity}


class IncidentManager:
    def __init__(self, quiet: float, next_id: int = 1):
        self.quiet = quiet
        self.next_id = next_id
        self.open: dict[tuple[str, str], Incident] = {}
        self.closed: list[Incident] = []  # en yenisi başta
        self.counts: dict[str, int] = {}  # türüne göre yeni bulgu sayısı (açılıştan beri)

    def get(self, incident_id: int) -> Incident | None:
        return next((i for i in [*self.open.values(), *self.closed] if i.id == incident_id), None)

    def update(self, findings: list[Finding], now: float, engine: Engine) -> list[tuple[str, Incident]]:
        """Döngü başına bir kez çağrılır. (olay türü, olay) listesi döner: opened | escalated | closed."""
        events: list[tuple[str, Incident]] = []
        groups: dict[tuple[str, str], list[Finding]] = defaultdict(list)
        for f in findings:
            groups[f.group].append(f)

        for group, fs in groups.items():
            inc = self.open.get(group)
            is_new = inc is None
            if inc is None:
                inc = self.open[group] = Incident(self.next_id, fs[0].scope, fs[0].namespace, fs[0].workload, now, now)
                self.next_id += 1
            before = inc.severity
            for f in fs:
                prev = inc.findings.get(f.key)
                if prev is None:
                    self.counts[f.kind] = self.counts.get(f.kind, 0) + 1
                inc.findings[f.key] = asdict(f) | {"first_seen": prev["first_seen"] if prev else now, "last_seen": now}
                inc.severity = max(inc.severity, f.severity)
            inc.last_seen = now
            if inc.summary.get("source") != "llm":
                inc.summary = rules.summary(inc.title, inc.findings)
            inc.commands = rules.commands(inc.scope, inc.namespace, inc.workload, inc.findings)
            if is_new:
                events.append(("opened", inc))
            elif inc.severity > before:
                events.append(("escalated", inc))

        for group, inc in list(self.open.items()):
            for f in inc.findings.values():
                f["active"] = f["last_seen"] == now
                if f["signal"]:  # grafik, bulgu sönse de olay kapanana kadar güncellenir
                    ent = f["pod"] or f["workload"]
                    chart = engine.chart(f["signal"], f["namespace"], ent, now)
                    if chart:  # aynı seride birden çok bulgu varsa en erken tespit anı ve varsa normal seviye korunur
                        old = inc.charts.get(f"{f['signal']}|{ent}", {})
                        inc.charts[f"{f['signal']}|{ent}"] = chart | {
                            "baseline": f["detail"].get("baseline") or old.get("baseline"),
                            "since": min(f["first_seen"], old.get("since", f["first_seen"]))}
            if now - inc.last_seen > self.quiet:
                inc.closed = now
                del self.open[group]
                self.closed.insert(0, inc)
                del self.closed[50:]
                events.append(("closed", inc))
        return events
