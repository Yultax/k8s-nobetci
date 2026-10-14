"""Canlı ekran sunucusu: tek sayfalık arayüz (static/index.html), JSON API ve /metrics."""
from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_INDEX = os.path.join(os.path.dirname(__file__), "static", "index.html")
_SEV = {1: "info", 2: "warning", 3: "critical"}


def metrics(s: dict) -> str:
    """Prometheus'un kazıyabileceği öz ölçümler; anomali skorunu Grafana'da çizmek için."""
    p = s.get("pipeline", {})
    lines = ["# TYPE nobetci_open_incidents gauge"]
    for sev, name in _SEV.items():
        lines.append(f'nobetci_open_incidents{{severity="{name}"}} {sum(1 for i in s.get("open", []) if i["severity"] == sev)}')
    lines += ["# TYPE nobetci_series gauge", f'nobetci_series {p.get("source", {}).get("series", 0)}',
              "# TYPE nobetci_cycle_seconds gauge", f'nobetci_cycle_seconds {p.get("cycle_seconds", 0)}',
              "# TYPE nobetci_suppressed_total counter", f'nobetci_suppressed_total {p.get("detect", {}).get("suppressed_total", 0)}',
              "# TYPE nobetci_llm_calls_total counter", f'nobetci_llm_calls_total {s["llm"]["calls"]}',
              "# TYPE nobetci_anomaly_score gauge"]
    for inc in s.get("open", []):
        for f in inc["findings"].values():
            if f.get("active") and "score" in f["detail"]:
                lines.append(f'nobetci_anomaly_score{{signal="{f["signal"]}",namespace="{f["namespace"]}",'
                             f'target="{f["pod"] or f["workload"]}"}} {f["detail"]["score"]}')
    return "\n".join(lines) + "\n"


def serve(app) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path == "/healthz":
                return self._send("ok", "text/plain")
            if url.path == "/":
                with open(_INDEX, encoding="utf-8") as f:
                    return self._send(f.read(), "text/html")
            if url.path == "/api/ping":
                return self._json(app.ping())
            if url.path == "/api/state":
                return self._json(app.state())
            if url.path == "/api/series":
                return self._json(app.series(q.get("k", "")))
            if url.path == "/api/incident" and q.get("id", "").isdigit():
                return self._json(app.incident(int(q["id"])))
            if url.path == "/metrics":
                return self._send(metrics(app.state()), "text/plain; version=0.0.4")
            self.send_error(404)

        def _json(self, data):
            if data is None:
                return self.send_error(404)
            self._send(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "application/json")

        def _send(self, body: str, ctype: str):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    ThreadingHTTPServer((app.cfg.bind, app.cfg.port), Handler).serve_forever()
