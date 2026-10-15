#!/usr/bin/env python3
"""Sahte Prometheus: küme olmadan geliştirme ve demo için.

Nöbetçinin sorduğu sorguları tanır ve zamana bağlı, tekrarlanabilir yapay seriler döner.
Senaryo, ilk isteğin zamanına (T0) göre işler:
  her gün aynı saatte  demo/rapor  CPU sıçraması (olağan iş; bastırılmalı)
  T0+20 dk             demo/api    bellek sızıntısı (limit 512 MiB)
  T0+25 dk             demo/web    CPU sıçraması (12 dk)
  T0+30 dk             demo/worker OOMKilled + CrashLoopBackOff
  T0+35 dk             demo/cache  gelen trafik kesilir
  T0+45 dk             demo/kuyruk-isci  CPU %20 artar, gelen trafik %40 düşer (15 dk). Tek tek hiçbir
                       eşiği aşmaz; normalde ikisi birlikte artıp azaldığı için yalnızca ML yakalar.

GET istekleri sahte Kubernetes API'si gibi cevaplanır (tools/sahte_kube.py); nöbetçiye
NOBETCI_KUBE_URL=http://127.0.0.1:<port> verilirse keşif ekranındaki küme haritası da dolar.

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
from sahte_kube import respond  # noqa: E402

NODE = "demo-node"
#        pod                          cpu    bellek MiB  ağ B/s
PODS = {
    "web-7d9f8b6c5d-x2k4p":    (0.12, 180, 400e3),
    "web-7d9f8b6c5d-r8t2v":    (0.11, 170, 380e3),
    "api-6b8c9d7f5-q9w8z":     (0.20, 200, 300e3),
    "worker-5c7d8f9b6-m4n5p":  (0.30, 150, 20e3),
    "cache-0":                 (0.05, 300, 800e3),
    "rapor-6f7c8d9b2-k5l6m":   (0.02, 90, 5e3),
    "kuyruk-isci-7b6c5d8f9-z4x7q": (0.6, 120, 2e6),
}
#         pod                                       namespace             cpu    bellek  ağ
SYSTEM = {
    "coredns-5d78c9869d-9xk2v":               ("kube-system",        0.01,  25,  10e3),
    "coredns-5d78c9869d-h7mqs":               ("kube-system",        0.01,  24,  9e3),
    "etcd-demo-node":                         ("kube-system",        0.06,  110, 50e3),
    "kube-apiserver-demo-node":               ("kube-system",        0.12,  420, 300e3),
    "kube-scheduler-demo-node":               ("kube-system",        0.01,  40,  2e3),
    "kube-controller-manager-demo-node":      ("kube-system",        0.03,  90,  8e3),
    "kube-proxy-x7k2m":                       ("kube-system",        0.002, 20,  1e3),
    "kindnet-p4r8t":                          ("kube-system",        0.002, 15,  1e3),
    "local-path-provisioner-6f8956fb48-2bqzl": ("local-path-storage", 0.002, 12,  1e3),
    "prometheus-server-7c9d5f8b6-t2wq9":      ("monitoring",         0.08,  380, 120e3),
    "kube-state-metrics-6d4f8b9c7-r5n2k":     ("monitoring",         0.01,  30,  40e3),
    "nobetci-5f6d7c8b9-v4m2x":                ("nobetci",            0.01,  45,  20e3),
    "alarm-api-7c9d4b8f6-k2x8p":              ("alarm-ornek",        0.002, 3,   2e3),
    "alarm-api-7c9d4b8f6-w5n7r":              ("alarm-ornek",        0.002, 3,   2e3),
    "role-gecidi-5f8b6c7d9-h4j6m":            ("alarm-ornek",        0.002, 2,   1e3),
    "mobil-uygulama-6d7f8c9b5-p2q4s":         ("alarm-ornek",        0.002, 2,   1e3),
}
NS = {p: "demo" for p in PODS} | {p: v[0] for p, v in SYSTEM.items()}
VALUES = PODS | {p: v[1:] for p, v in SYSTEM.items()}
T0 = None
LAST_T = None  # nöbetçinin en son sorduğu (sanal) zaman; sahte Kubernetes API senaryoyu buna göre oynatır


def noise(*key) -> float:
    """[-1, 1] aralığında, anahtara bağlı sabit gürültü."""
    d = hashlib.blake2b(repr(key).encode(), digest_size=4).digest()
    return int.from_bytes(d, "big") / 2**31 - 1


def pod_value(sig: str, pod: str, t: float) -> float | None:
    cpu, mem, net = VALUES[pod]
    day = 1 + 0.25 * math.sin(2 * math.pi * (t % 86400) / 86400)
    n = noise(sig, pod, int(t // 60))
    dt = (t - T0) / 60  # senaryo dakikası
    if pod.startswith("kuyruk"):  # işlediği iş kadar CPU yer: CPU ile gelen trafik birlikte hareket eder
        load = 1 + 0.4 * math.sin(2 * math.pi * (t % 86400) / 86400)
        odd = 45 <= dt < 60
        if sig == "pod_cpu":
            return cpu * load * (1 + 0.03 * n) * (1.2 if odd else 1)
        if sig == "pod_mem":
            return mem * (1 + 0.01 * n) * MiB
        if sig == "pod_net_rx":
            return net * load * (1 + 0.03 * n) * (0.6 if odd else 1)
        return 60e3 * load * (1 + 0.03 * n)
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


def scenario_minute() -> float:
    return 0.0 if T0 is None or LAST_T is None else (LAST_T - T0) / 60


def evaluate(query: str, t: float) -> list[tuple[dict, float]]:
    dt = (t - T0) / 60
    for sig in GAUGES:
        if query != sig.query:
            continue
        if sig.scope == "node":
            pod_sig = "pod_cpu" if sig.name == "node_cpu" else "pod_mem"
            total = sum(pod_value(pod_sig, p, t) for p in VALUES)
            return [({"instance": NODE}, total + (0.3 if sig.name == "node_cpu" else 1500 * MiB))]
        return [({"namespace": NS[p], "pod": p}, pod_value(sig.name, p, t)) for p in VALUES]
    worker = {"namespace": "demo", "pod": "worker-5c7d8f9b6-m4n5p", "container": "worker"}
    if query == MEM_LIMIT_QUERY:
        return [({"namespace": "demo", "pod": "api-6b8c9d7f5-q9w8z"}, 512 * MiB),
                ({"namespace": "demo", "pod": "worker-5c7d8f9b6-m4n5p"}, 256 * MiB)]
    if query.startswith("count by (__name__)"):  # keşif ekranının ölçüm kataloğu
        have = ("kube_node_status_condition", "kube_pod_status_reason", "kube_job_status_failed",
                "kube_deployment_status_observed_generation", "apiserver_request_total", "coredns_dns_responses_total",
                "kube_pod_container_resource_limits", "container_cpu_cfs_throttled_periods_total",
                "container_network_receive_packets_dropped_total", "etcd_disk_wal_fsync_duration_seconds_bucket")
        return [({"__name__": n}, 40 + 7 * i) for i, n in enumerate(have)]
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
    def do_GET(self):
        """Sahte Kubernetes API (NOBETCI_KUBE_URL ile): keşif ekranının küme haritası ve LLM bağlamı için."""
        path = urllib.parse.urlsplit(self.path).path
        body = respond(path, scenario_minute(), NODE, NS)
        if body is None:
            self.send_error(404)
        elif isinstance(body, str):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(body)

    def do_POST(self):
        global T0, LAST_T
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path.endswith("/chat/completions"):  # LLM yolunu denemek için hazır cevap veren sahte uç
            print("--- LLM'e giden kanıt ---\n" + json.loads(body)["messages"][-1]["content"], flush=True)
            text = "ÖZET: (sahte LLM) Kanıt metni alındı.\nNEDEN: Gerçek model bağlı değil.\nÖNERİ: NOBETCI_LLM_URL ile gerçek bir uç verin."
            return self._json({"choices": [{"message": {"content": text}}]})
        p = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        try:
            if self.path.endswith("/query"):
                t = float(p["time"])
                T0 = T0 or t
                LAST_T = t
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
