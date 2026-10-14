"""Canlı ekran sunucusu: tek sayfalık arayüz (static/index.html), JSON API ve /metrics."""
from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_STATIC = os.path.join(os.path.dirname(__file__), "static")
_PAGES = {"/": "index.html", "/kesif": "kesif.html", "/harita": "kesif.html"}
_SEV = {1: "info", 2: "warning", 3: "critical"}


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _lbl(**kw) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kw.items()) + "}"


def metrics(s: dict) -> str:
    """Prometheus biçiminde dışa aktarım (node exporter gibi kazınır): sağlık, her serinin normali ve
    sapması, açık olaylar, limit dolma tahminleri. Ham değerler zaten Prometheus'ta olduğu için tekrar verilmez."""
    out: list[str] = []

    def m(name: str, kind: str, help_: str, rows):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{lbl} {val}" for lbl, val in rows)

    p, llm = s.get("pipeline", {}), s["llm"]
    m("nobetci_build_info", "gauge", "Sürüm bilgisi.", [(_lbl(version=s.get("version", "")), 1)])
    m("nobetci_up", "gauge", "Son ölçüm turu hatasız tamamlandıysa 1.", [("", 0 if s.get("errors") else 1)])
    m("nobetci_last_cycle_timestamp_seconds", "gauge", "Son ölçüm turunun zamanı.", [("", round(s.get("now", 0), 3))])
    m("nobetci_cycle_seconds", "gauge", "Son ölçüm turunun süresi.", [("", p.get("cycle_seconds", 0))])
    m("nobetci_series", "gauge", "İzlenen seri sayısı.", [("", p.get("source", {}).get("series", 0))])
    m("nobetci_threshold", "gauge", "Sapma eşiği (normal ± eşik × ölçek).", [("", s.get("detector", {}).get("z", 0))])
    m("nobetci_baseline", "gauge", "Serinin normali: son 60 ölçümün medyanı.",
      [(_lbl(signal=sig, namespace=ns, target=t), round(med, 6)) for sig, ns, t, _, med, _ in s.get("exports", []) if med is not None])
    m("nobetci_deviation", "gauge", "Son değerin normalden sapması (ölçek cinsinden; + artış, − düşüş).",
      [(_lbl(signal=sig, namespace=ns, target=t), z) for sig, ns, t, _, _, z in s.get("exports", []) if z is not None])
    sev = {1: "info", 2: "warning", 3: "critical"}
    m("nobetci_open_incidents", "gauge", "Seviyesine göre açık olay sayısı.",
      [(_lbl(severity=name), sum(1 for i in s.get("open", []) if i["severity"] == k)) for k, name in sev.items()])
    m("nobetci_incident_open", "gauge", "Açık olay (değer her zaman 1; etiketler olayı tanımlar).",
      [(_lbl(id=i["id"], namespace=i["namespace"], workload=i["workload"], severity=sev[i["severity"]],
             kind=",".join(sorted({f["kind"] for f in i["findings"].values()}))), 1) for i in s.get("open", [])])
    m("nobetci_fill_eta_seconds", "gauge", "Bellek bu hızla limite kaç saniye sonra ulaşır.",
      [(_lbl(namespace=f["namespace"], target=f["pod"]), f["detail"]["eta"])
       for i in s.get("open", []) for f in i["findings"].values() if f["kind"] == "fill" and f.get("active")])
    m("nobetci_findings_total", "counter", "Türüne göre bulgu sayısı (süreç başladığından beri).",
      [(_lbl(kind=k), v) for k, v in sorted(s.get("finding_counts", {}).items())])
    m("nobetci_suppressed_total", "counter", "Saatlik profil yüzünden olağan sayılan sıçramalar.",
      [("", p.get("detect", {}).get("suppressed_total", 0))])
    m("nobetci_llm_calls_total", "counter", "LLM çağrıları.", [("", llm["calls"])])
    m("nobetci_llm_failures_total", "counter", "Başarısız LLM çağrıları.", [("", llm["failures"])])
    return "\n".join(out) + "\n"


def alerts(s: dict) -> list[dict]:
    """Başka sistemlerin okuyabileceği sade alarm listesi (açık olaylar)."""
    sev = {1: "bilgi", 2: "uyarı", 3: "kritik"}
    return [{"id": i["id"], "seviye": sev[i["severity"]], "baslik": i["title"], "kapsam": i["entity"],
             "acilis": round(i["opened"]), "son_gorulme": round(i["last_seen"]),
             "bulgular": [{"tur": f["kind"], "hedef": f["pod"] or f["workload"], "aciklama": f["title"], "suruyor": f.get("active", False)}
                          for f in i["findings"].values()],
             "ozet": i["summary"], "komutlar": i["commands"]} for i in s.get("open", [])]


def serve(app) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path == "/healthz":
                return self._send("ok", "text/plain")
            if url.path in _PAGES:
                with open(os.path.join(_STATIC, _PAGES[url.path]), encoding="utf-8") as f:
                    return self._send(f.read(), "text/html")
            if url.path == "/api/topoloji":
                return self._json(app.topology())
            if url.path == "/api/olcumler":
                return self._json(app.catalog())
            if url.path == "/api/ping":
                return self._json(app.ping())
            if url.path == "/api/state":
                return self._json(app.state())
            if url.path == "/api/series":
                return self._json(app.series(q.get("k", "")))
            if url.path == "/api/incident" and q.get("id", "").isdigit():
                return self._json(app.incident(int(q["id"])))
            if url.path in ("/api/alarmlar", "/api/alerts"):
                return self._json(alerts(app.state()))
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
