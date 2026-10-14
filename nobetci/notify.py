"""Bildirimler: standart çıktı (kubectl logs -f ile izlenir) ve isteğe bağlı webhook."""
from __future__ import annotations

import json
import threading
from collections import Counter
import time
import urllib.request

from .config import Config
from .incident import Incident

SEVERITY = {1: "BİLGİ", 2: "UYARI", 3: "KRİTİK"}
EVENT = {"opened": "YENİ OLAY", "escalated": "CİDDİLEŞTİ", "closed": "KAPANDI", "summary": "YORUM"}


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.counts: Counter[str] = Counter()

    def emit(self, event: str, inc: Incident, now: float) -> None:
        clock = time.strftime("%H:%M:%S", time.gmtime(now + self.cfg.tz_offset * 3600))
        lines = [f"{clock} {EVENT[event]} #{inc.id} [{SEVERITY[inc.severity]}] {inc.title}"]
        if event == "summary":
            s = inc.summary
            lines += [f"    özet : {s['ozet']}", f"    neden: {s['neden']}", f"    öneri: {s['oneri']}"]
        text = "\n".join(lines)
        print(text, flush=True)
        self.counts["terminal"] += 1
        if self.cfg.webhook_url:
            payload = {"text": text, "event": event, "cluster": self.cfg.cluster, "incident": inc.to_dict() | {"charts": {}}}
            threading.Thread(target=self._post, args=(payload,), daemon=True).start()

    def _post(self, payload: dict) -> None:
        req = urllib.request.Request(self.cfg.webhook_url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
            self.counts["webhook_ok"] += 1
        except OSError as e:
            self.counts["webhook_failed"] += 1
            print(f"webhook gönderilemedi: {e}", flush=True)
