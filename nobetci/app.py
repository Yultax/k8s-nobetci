"""Uygulama döngüsü: topla → tespit et → olaylara işle → gerekiyorsa LLM'e yorumlat."""
from __future__ import annotations

import queue
import threading
import time
import traceback

from .config import Clock, Config
from .engine import Engine
from .incident import IncidentManager
from .kube import Kube
from .llm import LLM, LLMError, build_context
from .notify import Notifier
from .prom import Prom
from .signals import fmt
from .store import Store


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.clock = Clock(cfg.time_speed)
        self.engine = Engine(cfg, Prom(cfg.prom_url, timeout=60))
        self.kube = Kube.in_cluster()
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
        nodes = sorted({ent for (name, _, ent) in e.series if name == "node_cpu"})
        top = [{"namespace": ns, "pod": pod, "cpu": cpu, "mem": e.last("pod_mem", ns, pod)} for ns, pod, cpu in e.top("pod_cpu")]
        return {
            "ready": True, "now": now, "cycle_seconds": round(seconds, 2), "errors": list(e.errors),
            "series": len(e.series), "pods": sum(1 for k in e.series if k[0] == "pod_cpu"),
            "profiles": len(e.profiles), "suppressed": e.suppressed,
            "nodes": [c for n in nodes for c in (e.chart("node_cpu", "", n, now), e.chart("node_mem", "", n, now)) if c],
            "top": top,
        }

    def state(self) -> dict:
        """Web ve /metrics için tutarlı, kopyalanmış durum."""
        with self.lock:
            view = dict(self._view)
            view["open"] = [i.to_dict() for i in sorted(self.manager.open.values(), key=lambda i: (-i.severity, i.opened))]
            view["closed"] = [i.to_dict() | {"charts": {}} for i in self.manager.closed[:20]]
            view["count_24h"] = self.store.count_since(view["now"] - 86400)
        view["cluster"] = self.cfg.cluster
        view["tz"] = self.cfg.tz_offset
        view["kube"] = self.kube is not None
        view["llm"] = {"enabled": self.llm.enabled, "model": self.llm.model, "calls": self.llm.calls,
                       "failures": self.llm.failures, "error": self.llm.last_error, "busy": len(self._pending)}
        return view

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
                top = [f"{t['namespace']}/{t['pod']}: {fmt(t['cpu'], 'cores')} CPU, {fmt(t['mem'] or 0, 'bytes')} bellek"
                       for t in self._view.get("top", [])[:5]] if inc.scope == "node" else []
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
