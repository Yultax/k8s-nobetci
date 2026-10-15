"""Uygulama döngüsü: topla → tespit et → olaylara işle → gerekiyorsa LLM'e yorumlat."""
from __future__ import annotations

import queue
import threading
import time
import traceback

from . import __version__
from .config import Clock, Config
from .engine import WINDOW, Engine
from .incident import IncidentManager
from .kube import Kube
from .llm import LLM, LLMError, build_context
from .notify import Notifier
from .prom import Prom
from .signals import GAUGE, GAUGES, fmt, workload_of
from .store import Store
from . import topology

# Keşif ekranının "ne izleyebiliriz" bölümü: bu ölçümlerin kümedeki Prometheus'ta olup olmadığı denetlenir.
CATALOG = ("kubelet_volume_stats_used_bytes", "kube_node_status_condition", "node_filesystem_avail_bytes",
           "kube_pod_status_reason", "kube_horizontalpodautoscaler_spec_max_replicas",
           "container_network_receive_packets_dropped_total", "coredns_dns_responses_total", "apiserver_request_total",
           "etcd_disk_wal_fsync_duration_seconds_bucket", "kubelet_pod_start_duration_seconds_bucket",
           "kube_job_status_failed", "kube_deployment_status_observed_generation", "http_requests_total",
           "kube_pod_container_resource_limits", "container_cpu_cfs_throttled_periods_total")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.clock = Clock(cfg.time_speed)
        self.engine = Engine(cfg, Prom(cfg.prom_url, timeout=60))
        self.kube = Kube.in_cluster() or (Kube(cfg.kube_url, "", None) if cfg.kube_url else None)
        self.store = Store(cfg.db)
        opened, closed, next_id = self.store.load()
        self.manager = IncidentManager(cfg.quiet, next_id)
        self.manager.open = {i.group: i for i in opened}
        self.manager.closed = closed
        self.llm = LLM(cfg)
        self.notifier = Notifier(cfg)
        # Kilit olay durumunu korur (döngü, LLM işçisi ve web aynı anda dokunur).
        # Engine'e yalnızca döngü dokunur; web onun hazır kopyasını (self._view) okur.
        self.lock = threading.RLock()
        self._jobs: queue.Queue[int] = queue.Queue()
        self._pending: set[int] = set()
        self._view: dict = {"now": self.clock.now(), "ready": False}
        self._charts: dict[str, dict] = {}
        self._ml: dict[str, dict] = {}
        self._cycle = 0
        self._topo_lock = threading.Lock()
        self._topo: tuple[float, dict | None] = (0.0, None)
        self._catalog: tuple[float, dict] = (0.0, {})
        self._catalog_lock = threading.Lock()

    def cycle(self, now: float) -> None:
        started = time.time()
        self.engine.collect(now)
        findings = self.engine.detect(now)
        with self.lock:
            for event, inc in self.manager.update(findings, now, self.engine):
                self.notifier.emit(event, inc, now)
                if event == "closed":
                    self.store.save(inc)
            for inc in self.manager.open.values():
                self.store.save(inc)
                due = (self.llm.enabled and inc.id not in self._pending and inc.signature != inc.llm_sig
                       and now - inc.opened >= self.cfg.interval and now - inc.llm_try >= self.cfg.llm_cooldown)
                if due:  # ilk döngüde değil: ilişkili bulgular aynı olaya toplansın
                    inc.llm_try = now
                    self._pending.add(inc.id)
                    self._jobs.put(inc.id)
            self._view = self._engine_view(now, time.time() - started)

    def _engine_view(self, now: float, seconds: float) -> dict:
        e = self.engine
        self._cycle += 1
        charts = {}
        for name, ns, ent in list(e.series):
            c = e.chart(name, ns, ent, now)
            if c:
                charts[f"{name}|{ns}|{ent}"] = c
        self._charts = charts
        self._ml = {f"{ns}|{wl}": e.ml_detail(ns, wl) for ns, wl in list(e.models)}
        pods = sorted({(ns, ent) for (name, ns, ent) in e.series if GAUGE[name].scope == "pod"})
        nodes = sorted({ent for (name, _, ent) in e.series if name == "node_cpu"})
        table = []
        for ns, pod in pods:
            row = {"ns": ns, "pod": pod, "workload": workload_of(pod)}
            for sig in ("pod_cpu", "pod_mem", "pod_net_rx", "pod_net_tx"):
                row[sig] = e.last(sig, ns, pod)
                z = e.last_z(sig, ns, pod)
                row[sig + "_z"] = None if z is None else round(z, 1)
            row["limit"] = e.limits.get((ns, pod))
            table.append(row)
        exports = []
        for (name, ns, ent), s in e.series.items():
            if s.vs:
                z = s.z(len(s.vs) - 1)
                exports.append((name, ns, ent, s.vs[-1], s.med[-1], None if z is None else round(z, 2)))
        st = e.stats
        return {
            "exports": exports, "limits": [(ns, pod, v) for (ns, pod), v in e.limits.items()],
            "ready": True, "cycle": self._cycle, "now": now, "interval": self.cfg.interval,
            "detector": {"z": self.cfg.z, "persist": self.cfg.persist, "window": WINDOW, "warmup": self.cfg.warmup,
                         "quiet": self.cfg.quiet, "fill_horizon": self.cfg.fill_horizon},
            "errors": list(e.errors),
            "pipeline": {
                "source": {"url": self.cfg.prom_url, "series": len(e.series), "pods": len(pods), "nodes": len(nodes),
                           "profiles": len(e.profiles), "queries": st.get("queries", 0),
                           "query_ms": round(st.get("query_ms", 0))},
                "detect": {"candidates": st.get("candidates", 0), "suppressed": st.get("suppressed", 0),
                           "suppressed_total": e.suppressed, "fills": st.get("fills", 0),
                           "checks": st.get("checks", {}), "findings": st.get("findings", 0),
                           "ml_scored": st.get("ml_scored", 0), "ml_flags": st.get("ml_flags", 0)},
                "cycle_seconds": round(seconds, 3),
            },
            "ml": e.ml_view(),
            "heat": {sig.name: e.heat(sig.name) for sig in GAUGES if sig.scope == "pod"},
            "nodes": [c for n in nodes for c in (charts.get(f"node_cpu||{n}"), charts.get(f"node_mem||{n}")) if c],
            "pods": table,
        }

    def ping(self) -> dict:
        return {"ready": self._view.get("ready", False), "cycle": self._view.get("cycle", 0), "now": self.clock.now()}

    def series(self, key: str) -> dict | None:
        return self._charts.get(key)

    def ml(self, key: str) -> dict | None:
        return self._ml.get(key)

    def incident(self, incident_id: int) -> dict | None:
        with self.lock:
            inc = self.manager.get(incident_id) or self.store.get(incident_id)
            return inc.to_dict() if inc else None

    def state(self) -> dict:
        """Ekran ve /metrics için tutarlı, kopyalanmış durum."""
        with self.lock:
            view = dict(self._view)
            view["open"] = [i.to_dict() for i in sorted(self.manager.open.values(), key=lambda i: (-i.severity, i.opened))]
            view["closed"] = [i.to_dict() | {"charts": {}} for i in self.manager.closed if i.closed >= view["now"] - 86400]
            view["count_24h"] = self.store.count_since(view["now"] - 86400)
            view["finding_counts"] = dict(self.manager.counts)
        view["cluster"] = self.cfg.cluster
        view["version"] = __version__
        view["tz"] = self.cfg.tz_offset
        view["kube"] = self.kube is not None
        view["notify"] = dict(self.notifier.counts) | {"webhook": bool(self.cfg.webhook_url)}
        view["llm"] = {"enabled": self.llm.enabled, "model": self.llm.model, "calls": self.llm.calls,
                       "failures": self.llm.failures, "error": self.llm.last_error, "busy": len(self._pending)}
        return view

    def topology(self) -> dict:
        """Küme haritası. Kubernetes API en fazla 8 sn'de bir sorulur; aynı anda tek istek gider."""
        with self._topo_lock:
            at, snap = self._topo
            if self.kube and time.time() - at > 8:
                snap = self.kube.snapshot()
                self._topo = (time.time(), snap)
        return topology.build(snap, self.state())

    def catalog(self) -> dict:
        """Önerilen ölçümlerden hangileri kümedeki Prometheus'ta var (seri sayısı); 10 dk önbellekli."""
        with self._catalog_lock:
            at, data = self._catalog
            if time.time() - at > 600 or not data:
                query = 'count by (__name__) ({__name__=~"%s"})' % "|".join(CATALOG)
                try:
                    found = {m.get("__name__", ""): v for m, v in self.engine.prom.instant(query, self.clock.now())}
                    data = {"ok": True, "series": {n: int(found.get(n, 0)) for n in CATALOG}}
                except Exception as e:  # noqa: BLE001 - ekran için yalnızca hata metni yeterli
                    data = {"ok": False, "error": str(e)[:200], "series": {}}
                self._catalog = (time.time(), data)
        return data

    def _llm_loop(self) -> None:
        while True:
            inc_id = self._jobs.get()
            with self.lock:
                inc = self.manager.get(inc_id)
                if inc is None:
                    self._pending.discard(inc_id)
                    continue
                data, sig = inc.to_dict(), inc.signature
                others = [i.title for i in self.manager.open.values() if i.id != inc_id]
                pods = sorted(self._view.get("pods", []), key=lambda r: -(r["pod_cpu"] or 0))[:5]
                top = [f"{t['ns']}/{t['pod']}: {fmt(t['pod_cpu'] or 0, 'cores')} CPU, {fmt(t['pod_mem'] or 0, 'bytes')} bellek"
                       for t in pods] if inc.scope == "node" else []
            summary = None
            try:
                summary = self.llm.summarise(build_context(data, others, top, self.kube, self.cfg, data["last_seen"]))
            except LLMError as e:
                print(f"LLM yorumu alınamadı (#{inc_id}): {e}", flush=True)
            except Exception:
                traceback.print_exc()
            with self.lock:
                self._pending.discard(inc_id)
                inc = self.manager.get(inc_id)
                if summary and inc:
                    inc.summary = summary
                    inc.llm_sig = sig
                    self.store.save(inc)
                    self.notifier.emit("summary", inc, self.clock.now())

    def run(self) -> None:
        from .web import serve
        threading.Thread(target=serve, args=(self,), daemon=True).start()
        threading.Thread(target=self._llm_loop, daemon=True).start()
        print(f"nöbetçi başladı: küme={self.cfg.cluster} prometheus={self.cfg.prom_url} "
              f"llm={self.llm.model if self.llm.enabled else 'kapalı'} kube-api={'var' if self.kube else 'yok'} "
              f"kayıt={self.store.path} ekran=:{self.cfg.port}", flush=True)
        self.engine.backfill(self.clock.now())
        print(f"geçmiş yüklendi: {len(self.engine.series)} seri", flush=True)
        while True:
            now = self.clock.now()
            try:
                self.cycle(now)
                for err in self.engine.errors[:3]:
                    print(f"prometheus hatası: {err}", flush=True)
            except Exception:
                traceback.print_exc()
            self.clock.sleep(self.cfg.interval - (self.clock.now() - now))
