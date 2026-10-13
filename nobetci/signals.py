"""İzlenen sinyaller: istatistiksel tespit yapılan ölçümler ve durum kontrolleri."""
from __future__ import annotations

import re
from dataclasses import dataclass

MiB = 1024 * 1024
_C = 'container!="",container!="POD"'


@dataclass(frozen=True)
class Signal:
    name: str
    title: str      # Türkçe ad, cümle içinde kullanılır
    unit: str       # cores | bytes | bps
    query: str
    min_abs: float  # bundan küçük değişimler anomali sayılmaz
    scope: str = "pod"  # pod | node


GAUGES = [
    Signal("pod_cpu", "CPU kullanımı", "cores",
           f"sum by (namespace,pod) (irate(container_cpu_usage_seconds_total{{{_C}}}[5m]))", 0.1),
    Signal("pod_mem", "bellek kullanımı", "bytes",
           f"sum by (namespace,pod) (container_memory_working_set_bytes{{{_C}}})", 64 * MiB),
    Signal("pod_net_rx", "gelen ağ trafiği", "bps",
           'sum by (namespace,pod) (irate(container_network_receive_bytes_total{pod!=""}[5m]))', 100e3),
    Signal("pod_net_tx", "giden ağ trafiği", "bps",
           'sum by (namespace,pod) (irate(container_network_transmit_bytes_total{pod!=""}[5m]))', 100e3),
    Signal("node_cpu", "node CPU kullanımı", "cores",
           'sum by (instance) (irate(container_cpu_usage_seconds_total{id="/"}[5m]))', 0.5, "node"),
    Signal("node_mem", "node bellek kullanımı", "bytes",
           'sum by (instance) (container_memory_working_set_bytes{id="/"})', 512 * MiB, "node"),
]
GAUGE = {s.name: s for s in GAUGES}

MEM_LIMIT_QUERY = 'sum by (namespace,pod) (kube_pod_container_resource_limits{resource="memory"})'


@dataclass(frozen=True)
class Check:
    kind: str
    severity: int   # 1 bilgi, 2 uyarı, 3 kritik
    query: str
    scope: str = "pod"  # pod | deploy | node


_RESTARTS = "increase(kube_pod_container_status_restarts_total[10m]) > 0"
CHECKS = [
    Check("oom", 3,
          '(kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1)'
          f" and on (namespace,pod,container) ({_RESTARTS})"),
    Check("waiting", 3,
          "kube_pod_container_status_waiting_reason{reason=~"
          '"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|InvalidImageName"} == 1'),
    Check("restart", 2, _RESTARTS),
    Check("pending", 2, 'min_over_time(kube_pod_status_phase{phase="Pending"}[10m]) == 1'),
    Check("unavailable", 2, "min_over_time(kube_deployment_status_replicas_unavailable[10m]) > 0", "deploy"),
    Check("node_notready", 3, 'kube_node_status_condition{condition="Ready",status="true"} == 0', "node"),
    Check("throttle", 1,
          "sum by (namespace,pod) (rate(container_cpu_cfs_throttled_periods_total[5m]))"
          " / sum by (namespace,pod) (rate(container_cpu_cfs_periods_total[5m])) > 0.5"),
]

# Kubernetes'in ürettiği rastgele son eklerin alfabesi (sesli harf içermez).
_A = "bcdfghjklmnpqrstvwxz2456789"
_PATTERNS = [
    re.compile(rf"^(.+)-[{_A}]{{6,10}}-[{_A}]{{5}}$"),  # Deployment -> ReplicaSet -> Pod
    re.compile(rf"^(.+)-[{_A}]{{5}}$"),                 # DaemonSet / Job
    re.compile(r"^(.+)-\d+$"),                          # StatefulSet
]


def workload_of(pod: str) -> str:
    """Pod adından iş yükü adını çıkarır; pod yeniden doğunca geçmişi kaybetmemek için."""
    for rx in _PATTERNS:
        m = rx.match(pod)
        if m:
            return m.group(1)
    return pod


def label(labels: dict, name: str) -> str:
    """honor_labels kapalıysa Prometheus hedef etiketini exported_ önekiyle saklar."""
    return labels.get("exported_" + name) or labels.get(name, "")


def fmt(value: float, unit: str) -> str:
    if unit == "cores":
        return f"{value * 1000:.0f}m" if value < 1 else f"{value:.2f} çekirdek"
    if unit == "bytes":
        return f"{value / MiB:.0f} MiB" if value < 1024 * MiB else f"{value / MiB / 1024:.2f} GiB"
    if unit == "bps":
        return f"{value / 1e3:.0f} KB/s" if value < 1e6 else f"{value / 1e6:.1f} MB/s"
    return f"{value:.2f}"


def fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "1 dk'dan az"
    if m < 60:
        return f"{m} dk"
    return f"{m // 60} sa {m % 60} dk" if m % 60 else f"{m // 60} sa"
