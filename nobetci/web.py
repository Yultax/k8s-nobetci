"""Canlı ekran: sunucuda üretilen tek sayfa (betik yok, kendini yeniler), JSON durumu ve /metrics."""
from __future__ import annotations

import json
import time
from html import escape as h
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .signals import fmt, fmt_duration

SEV = {1: ("info", "i", "Bilgi"), 2: ("warning", "▲", "Uyarı"), 3: ("critical", "!", "Kritik")}

CSS = """
:root{color-scheme:light;--page:#f4f3ef;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8982;
--grid:#e6e5e0;--band:rgba(82,81,78,.10);--series:#2a78d6;--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;--info:#8a8982}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#111110;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#8a8982;--grid:#33332f;--band:rgba(195,194,183,.12);--series:#3987e5}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;justify-content:space-between;margin-bottom:16px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:28px 0 10px}h3{font-size:15px;margin:0}
.meta{color:var(--ink2);font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile,.card{background:var(--surface);border:1px solid var(--grid);border-radius:8px}
.tile{padding:12px 14px}.tile b{display:block;font-size:26px;font-variant-numeric:tabular-nums;line-height:1.2}
.tile span{color:var(--ink2);font-size:12px}
.card{padding:16px;margin-bottom:12px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;white-space:nowrap}
.chip i{display:inline-grid;place-items:center;width:16px;height:16px;border-radius:50%;color:#fff;font:700 10px/1 system-ui;font-style:normal}
.chip .critical{background:var(--critical)}.chip .warning{background:var(--warning);color:#0b0b0b}
.chip .info{background:var(--info)}.chip .good{background:var(--good)}
.head{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline}
.findings{margin:10px 0 0;padding:0;list-style:none}.findings li{padding:3px 0;display:flex;gap:10px;align-items:baseline}
.findings .gone{color:var(--muted)}
.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:12px;margin-top:12px}
figure{margin:0}figcaption{font-size:12px;color:var(--ink2);margin-bottom:2px;overflow-wrap:anywhere}
svg{display:block;width:100%;height:auto;font:11px system-ui,sans-serif}
svg .hit{fill:transparent}svg .hit:hover{fill:var(--band)}
.note{border-left:3px solid var(--grid);padding:2px 0 2px 12px;margin-top:14px}
.note p{margin:4px 0}.note .src{font-size:12px;color:var(--ink2)}
pre{background:var(--page);border-radius:6px;padding:10px 12px;margin:10px 0 0;overflow-x:auto;font:12px/1.5 ui-monospace,Menlo,monospace}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid);white-space:nowrap}th{font-size:12px;color:var(--ink2);font-weight:500}
td.n,th.n{text-align:right}.scroll{overflow-x:auto}.empty{color:var(--ink2)}
"""


def clock(ts: float, tz: float, fmt_: str = "%H:%M") -> str:
    return time.strftime(fmt_, time.gmtime(ts + tz * 3600))


def chip(severity: int | str) -> str:
    cls, icon, text = SEV[severity] if isinstance(severity, int) else ("good", "✓", severity)
    return f'<span class="chip"><i class="{cls}">{icon}</i>{text}</span>'


def chart_svg(c: dict, tz: float) -> str:
    """Tek serili çizgi grafik. Kesikli çizgiler: normal seviye ve limit; gri şerit: bu saat için olağan aralık."""
    W, H, L, R, T, B = 460, 150, 4, 104, 16, 18
    ts, vs, unit = c["ts"], c["vs"], c["unit"]
    refs = [("normal", c.get("baseline")), ("limit", c.get("limit"))]
    top = max(vs + [v for _, v in refs if v])
    band = c.get("band")
    if band and band[1] <= 3 * top:
        top = max(top, band[1])
    else:
        band = None
    top = top * 1.1 or 1.0
    t0, t1 = ts[0], max(ts[-1], ts[0] + 1)
    x = lambda t: L + (t - t0) / (t1 - t0) * (W - L - R)
    y = lambda v: T + (1 - v / top) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{h(c["title"])}">']
    if band:
        out.append(f'<rect x="{L}" y="{y(band[1]):.1f}" width="{W - L - R}" height="{max(y(band[0]) - y(band[1]), 1):.1f}" fill="var(--band)"/>')
    for frac in (0, 0.5, 1):
        gy = y(top / 1.1 * frac)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="var(--grid)"/>')
        if frac == 1:
            out.append(f'<text x="{L}" y="{gy - 3:.1f}" fill="var(--muted)">{h(fmt(top / 1.1 * frac, unit))}</text>')
    labels = [(y(vs[-1]), fmt(vs[-1], unit), "var(--ink)", 600)]
    for name, v in refs:
        if v:
            out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--muted)" stroke-dasharray="4 3"/>')
            labels.append((y(v), f"{name} {fmt(v, unit)}", "var(--ink2)", 400))
    since = c.get("since")
    if since and t0 < since <= t1:
        out.append(f'<line x1="{x(since):.1f}" x2="{x(since):.1f}" y1="{T - 4}" y2="{H - B}" stroke="var(--muted)"/>')
        anchor = "end" if x(since) > (W - R) * 0.6 else "start"
        out.append(f'<text x="{x(since) + (-4 if anchor == "end" else 4):.1f}" y="{T - 6}" text-anchor="{anchor}" fill="var(--ink2)">tespit {clock(since, tz)}</text>')
    pts = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in zip(ts, vs))
    out.append(f'<polyline points="{pts}" fill="none" stroke="var(--series)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    out.append(f'<circle cx="{x(ts[-1]):.1f}" cy="{y(vs[-1]):.1f}" r="4" fill="var(--series)" stroke="var(--surface)" stroke-width="2"/>')
    prev = -99.0
    for ly, text, color, weight in sorted(labels):  # sağ kenar etiketleri üst üste binmesin
        ly = max(ly + 4, prev + 13)
        prev = ly
        out.append(f'<text x="{W - R + 10}" y="{ly:.1f}" fill="{color}" font-weight="{weight}">{h(text)}</text>')
    out.append(f'<text x="{L}" y="{H - 4}" fill="var(--muted)">{clock(t0, tz)}</text>')
    out.append(f'<text x="{W - R}" y="{H - 4}" text-anchor="end" fill="var(--muted)">{clock(t1, tz)}</text>')
    step = (W - L - R) / max(len(ts), 1)
    for t, v in zip(ts, vs):  # fareyle üzerine gelince değeri gösteren sütunlar
        out.append(f'<rect class="hit" x="{x(t) - step / 2:.1f}" y="{T}" width="{step:.1f}" height="{H - T - B}"><title>{clock(t, tz)} · {h(fmt(v, unit))}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def figure(c: dict, tz: float) -> str:
    return f'<figure><figcaption>{h(c["entity"])} · {h(c["title"])}</figcaption>{chart_svg(c, tz)}</figure>'


def incident_card(inc: dict, now: float, tz: float) -> str:
    out = [f'<article class="card"><div class="head">{chip(inc["severity"])}<h3>#{inc["id"]} {h(inc["title"])}</h3></div>',
           f'<div class="meta">{clock(inc["opened"], tz)}\'den beri açık · {fmt_duration(now - inc["opened"])}</div>',
           '<ul class="findings">']
    for f in sorted(inc["findings"].values(), key=lambda f: (-f["severity"], f["first_seen"])):
        gone = "" if f.get("active") else ' class="gone"'
        state = "" if f.get("active") else f' — {clock(f["last_seen"], tz)}\'de son görüldü'
        out.append(f'<li{gone}>{chip(f["severity"])}<span>{h(f["pod"] or f["workload"])}: {h(f["title"])}{state}</span></li>')
    out.append("</ul>")
    if inc["charts"]:
        out.append('<div class="charts">' + "".join(figure(c, tz) for c in inc["charts"].values()) + "</div>")
    s = inc["summary"]
    src = (f'{h(s.get("model", ""))} yorumu · {s.get("seconds", "?")} sn' if s.get("source") == "llm" else "Kural tabanlı özet")
    out.append(f'<div class="note"><p class="src">{src}</p><p>{h(s["ozet"])}</p>'
               f'<p><b>Olası neden:</b> {h(s["neden"])}</p><p><b>Öneri:</b> {h(s["oneri"])}</p></div>')
    if inc["commands"]:
        out.append("<pre>" + h("\n".join(inc["commands"])) + "</pre>")
    out.append("</article>")
    return "".join(out)


def page(s: dict, refresh: bool = True) -> str:
    tz = s["tz"]
    head = (f'<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            + ('<meta http-equiv="refresh" content="15">' if refresh else "")
            + f'<title>Nöbetçi · {h(s["cluster"])}</title><style>{CSS}</style></head><body><main>')
    if not s.get("ready"):
        return head + '<h1>Nöbetçi</h1><p class="empty">Prometheus geçmişi yükleniyor…</p></main></body></html>'
    now, opened = s["now"], s["open"]
    llm = s["llm"]
    llm_text = "kapalı (kural tabanlı özet)" if not llm["enabled"] else llm["model"] + (f' — hata: {llm["error"]}' if llm["error"] else "")
    status = chip("Sakin") if not opened else chip(max(i["severity"] for i in opened)).replace("</span>", f" · {len(opened)} açık olay</span>")
    out = [head, f'<header><h1>Nöbetçi · {h(s["cluster"])}</h1><div>{status}</div></header>',
           f'<div class="meta">Son ölçüm {clock(now, tz, "%H:%M:%S")} · döngü {s["cycle_seconds"]} sn · LLM: {h(llm_text)}'
           f' · Kubernetes API: {"bağlı" if s["kube"] else "yok"}</div>']
    for err in s["errors"][:2]:
        out.append(f'<p>{chip(3)} Prometheus hatası: {h(err)}</p>')
    out.append('<div class="tiles" style="margin-top:14px">'
               f'<div class="tile"><b>{len(opened)}</b><span>açık olay</span></div>'
               f'<div class="tile"><b>{s["count_24h"]}</b><span>son 24 saatte olay</span></div>'
               f'<div class="tile"><b>{s["pods"]}</b><span>izlenen pod · {s["series"]} seri</span></div>'
               f'<div class="tile"><b>{s["suppressed"]}</b><span>olağan sayılıp bastırılan sıçrama</span></div></div>')
    out.append("<h2>Açık olaylar</h2>")
    out.append("".join(incident_card(i, now, tz) for i in opened) or '<p class="empty">Açık olay yok.</p>')
    if s["nodes"]:
        out.append('<h2>Node</h2><div class="card"><div class="charts" style="margin-top:0">'
                   + "".join(figure(c, tz) for c in s["nodes"]) + "</div></div>")
    if s["top"]:
        rows = "".join(f'<tr><td>{h(t["namespace"])}</td><td>{h(t["pod"])}</td><td class="n">{fmt(t["cpu"], "cores")}</td>'
                       f'<td class="n">{fmt(t["mem"], "bytes") if t["mem"] is not None else "–"}</td></tr>' for t in s["top"])
        out.append('<h2>En çok CPU kullanan pod\'lar</h2><div class="card scroll"><table><tr><th>Namespace</th><th>Pod</th>'
                   f'<th class="n">CPU</th><th class="n">Bellek</th></tr>{rows}</table></div>')
    if s["closed"]:
        rows = "".join(f'<tr><td>#{i["id"]}</td><td>{chip(i["severity"])}</td><td>{h(i["title"])}</td>'
                       f'<td>{clock(i["opened"], tz, "%d.%m %H:%M")}</td><td class="n">{fmt_duration(i["closed"] - i["opened"])}</td></tr>'
                       for i in s["closed"])
        out.append('<h2>Kapanan olaylar</h2><div class="card scroll"><table><tr><th></th><th>Seviye</th><th>Olay</th>'
                   f'<th>Açılış</th><th class="n">Süre</th></tr>{rows}</table></div>')
    out.append("</main></body></html>")
    return "".join(out)


def metrics(s: dict) -> str:
    """Prometheus'un kazıyabileceği öz ölçümler; anomali skorunu Grafana'da çizmek için."""
    lines = ["# TYPE nobetci_open_incidents gauge"]
    for sev, (name, _, _) in SEV.items():
        lines.append(f'nobetci_open_incidents{{severity="{name}"}} {sum(1 for i in s.get("open", []) if i["severity"] == sev)}')
    lines += ["# TYPE nobetci_series gauge", f'nobetci_series {s.get("series", 0)}',
              "# TYPE nobetci_cycle_seconds gauge", f'nobetci_cycle_seconds {s.get("cycle_seconds", 0)}',
              "# TYPE nobetci_suppressed_total counter", f'nobetci_suppressed_total {s.get("suppressed", 0)}',
              "# TYPE nobetci_llm_calls_total counter", f'nobetci_llm_calls_total {s["llm"]["calls"]}',
              "# TYPE nobetci_llm_failures_total counter", f'nobetci_llm_failures_total {s["llm"]["failures"]}',
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
            path = self.path.split("?")[0]
            if path == "/healthz":
                return self._send("ok", "text/plain")
            state = app.state()
            if path == "/":
                self._send(page(state, refresh="static" not in self.path), "text/html")
            elif path == "/api/state":
                self._send(json.dumps(state, ensure_ascii=False), "application/json")
            elif path == "/metrics":
                self._send(metrics(state), "text/plain; version=0.0.4")
            else:
                self.send_error(404)

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
