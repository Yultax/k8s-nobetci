#!/usr/bin/env python3
"""Sahte Prometheus: küme olmadan geliştirme ve demo için.

Nöbetçinin sorduğu sorguları tanır ve zamana bağlı, tekrarlanabilir yapay seriler döner.
Senaryo, ilk isteğin zamanına (T0) göre işler:
  her gün aynı saatte  demo/rapor  CPU sıçraması (olağan iş; bastırılmalı)
  T0+20 dk             demo/api    bellek sızıntısı (limit 512 MiB)
  T0+25 dk             demo/web    CPU sıçraması (12 dk)
  T0+30 dk             demo/worker OOMKilled + CrashLoopBackOff
  T0+35 dk             demo/cache  gelen trafik kesilir

Kullanım: python3 tools/sahte_prometheus.py [port]
"""
import hashlib
import json
import math
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nobetci.signals import CHECKS, GAUGES, MEM_LIMIT_QUERY, MiB  # noqa: E402

NODE = "demo-node"
#        pod                          cpu    bellek MiB  ağ B/s
PODS = {
    "web-7d9f8b6c5d-x2k4p":    (0.12, 180, 400e3),
    "api-6b8c9d7f5-q9w8z":     (0.20, 200, 300e3),
    "worker-5c7d8f9b6-m4n5p":  (0.30, 150, 20e3),
    "cache-0":                 (0.05, 300, 800e3),
    "rapor-6f7c8d9b2-k5l6m":   (0.02, 90, 5e3),
}
SYSTEM = {"coredns-5d78c9869d-9xk2v": (0.01, 25, 10e3), "etcd-demo-node": (0.06, 110, 50e3)}
T0 = None


def noise(*key) -> float:
    """[-1, 1] aralığında, anahtara bağlı sabit gürültü."""
    d = hashlib.blake2b(repr(key).encode(), digest_size=4).digest()
    return int.from_bytes(d, "big") / 2**31 - 1


def pod_value(sig: str, pod: str, t: float) -> float | None:
    cpu, mem, net = {**PODS, **SYSTEM}[pod]
    day = 1 + 0.25 * math.sin(2 * math.pi * (t % 86400) / 86400)
    n = noise(sig, pod, int(t // 60))
    dt = (t - T0) / 60  # senaryo dakikası
    if sig == "pod_cpu":
        v = cpu * day * (1 + 0.08 * n)
        if pod.startswith("rapor") and 15 <= ((t - T0) % 86400) / 60 < 25:
            v += 0.6  # her gün aynı saatte çalışan rapor işi
        if pod.startswith("web") and 25 <= dt < 37:
            v += 0.9
        if pod.startswith("worker") and dt >= 30:
            v = 0.02
        return v
    if sig == "pod_mem":
        v = mem * (1 + 0.01 * n)
        if pod.startswith("api") and dt >= 20:
            v += min((dt - 20) * 4.0, 512 - mem)  # dakikada 4 MiB sızıntı
        if pod.startswith("worker") and dt >= 30:
            v = 30
        return v * MiB
    v = net * day * (1 + 0.1 * n)
    if pod.startswith("cache") and dt >= 35 and sig == "pod_net_rx":
        v *= 0.02
    return v


def evaluate(query: str, t: float) -> list[tuple[dict, float]]:
    dt = (t - T0) / 60
    for sig in GAUGES:
        if query != sig.query:
            continue
        if sig.scope == "node":
            pod_sig = "pod_cpu" if sig.name == "node_cpu" else "pod_mem"
            total = sum(pod_value(pod_sig, p, t) for p in {**PODS, **SYSTEM})
            return [({"instance": NODE}, total + (0.3 if sig.name == "node_cpu" else 1500 * MiB))]
        return [({"namespace": "demo", "pod": p}, pod_value(sig.name, p, t)) for p in PODS] + \
               [({"namespace": "kube-system", "pod": p}, pod_value(sig.name, p, t)) for p in SYSTEM]
    worker = {"namespace": "demo", "pod": "worker-5c7d8f9b6-m4n5p", "container": "worker"}
    if query == MEM_LIMIT_QUERY:
        return [({"namespace": "demo", "pod": "api-6b8c9d7f5-q9w8z"}, 512 * MiB),
                ({"namespace": "demo", "pod": "worker-5c7d8f9b6-m4n5p"}, 256 * MiB)]
    kind = next((c.kind for c in CHECKS if c.query == query), None)
    if kind is None:
        raise ValueError("tanınmayan sorgu: " + query)
    if dt >= 30:
        if kind == "oom":
            return [(worker | {"reason": "OOMKilled"}, 1)]
        if kind == "waiting":
            return [(worker | {"reason": "CrashLoopBackOff"}, 1)]
        if kind == "restart":
            return [(worker, 3.0)]
        if kind == "unavailable" and dt >= 40:
            return [({"namespace": "demo", "deployment": "worker"}, 1)]
    return []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global T0
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path.endswith("/chat/completions"):  # LLM yolunu denemek için hazır cevap veren sahte uç
            print("--- LLM'e giden kanıt ---\n" + json.loads(body)["messages"][1]["content"], flush=True)
            text = "ÖZET: (sahte LLM) Kanıt metni alındı.\nNEDEN: Gerçek model bağlı değil.\nÖNERİ: NOBETCI_LLM_URL ile gerçek bir uç verin."
            return self._json({"choices": [{"message": {"content": text}}]})
        p = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        try:
            if self.path.endswith("/query"):
                t = float(p["time"])
                T0 = T0 or t
                result = [{"metric": m, "value": [t, str(v)]} for m, v in evaluate(p["query"], t)]
            else:
                start, end, step = float(p["start"]), float(p["end"]), float(p["step"])
                T0 = T0 or end
                series: dict[str, dict] = {}
                t = start
                while t <= end:
                    for m, v in evaluate(p["query"], t):
                        s = series.setdefault(json.dumps(m, sort_keys=True), {"metric": m, "values": []})
                        s["values"].append([t, str(v)])
                    t += step
                result = list(series.values())
            out = {"status": "success", "data": {"result": result}}
        except (KeyError, ValueError) as e:
            out = {"status": "error", "error": str(e)}
        self._json(out)

    def _json(self, out: dict):
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9090
    print(f"sahte prometheus 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
