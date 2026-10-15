"""Sahte Kubernetes API cevapları (tools/sahte_prometheus.py GET isteklerinde kullanır).

Nesneler sahte Prometheus'taki pod listesinden üretilir ve aynı senaryoyu izler:
T0+30 dk'dan sonra demo/worker OOMKilled → CrashLoopBackOff, T0+40 dk'dan sonra Deployment'ı hazır değil.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nobetci.signals import workload_of  # noqa: E402

STATIC = ("etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager")
DAEMON = ("kube-proxy", "kindnet")
STATEFUL = ("cache",)
LABEL = {"coredns": {"k8s-app": "kube-dns"}, "kube-proxy": {"k8s-app": "kube-proxy"}}
IMAGE = {
    "web": "nginx:1.27", "api": "ghcr.io/ornek/api:2.4.1", "worker": "ghcr.io/ornek/worker:2.4.1",
    "cache": "redis:7.4", "rapor": "ghcr.io/ornek/rapor:1.0", "kuyruk-isci": "ghcr.io/ornek/kuyruk:1.3", "coredns": "registry.k8s.io/coredns/coredns:v1.11.3",
    "etcd": "registry.k8s.io/etcd:3.5.15-0", "kube-apiserver": "registry.k8s.io/kube-apiserver:v1.31.0",
    "kube-scheduler": "registry.k8s.io/kube-scheduler:v1.31.0",
    "kube-controller-manager": "registry.k8s.io/kube-controller-manager:v1.31.0",
    "kube-proxy": "registry.k8s.io/kube-proxy:v1.31.0", "kindnet": "docker.io/kindest/kindnetd:v20240813",
    "local-path-provisioner": "docker.io/kindest/local-path-provisioner:v20240813",
    "prometheus-server": "quay.io/prometheus/prometheus:v2.54.1",
    "kube-state-metrics": "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0",
    "nobetci": "nobetci:yerel",
}
LIMITS = {"alarm-api": "32Mi", "role-gecidi": "32Mi", "mobil-uygulama": "32Mi", "api": "512Mi", "worker": "256Mi", "web": "256Mi", "nobetci": "384Mi", "prometheus-server": "1Gi"}
BOOT = time.time() - 3 * 86400


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _wl(pod: str, node: str) -> tuple[str, str]:
    """(iş yükü türü, adı)"""
    for s in STATIC:
        if pod == f"{s}-{node}":
            return "Static", s
    w = workload_of(pod)
    if w in DAEMON:
        return "DaemonSet", w
    if w in STATEFUL:
        return "StatefulSet", w
    return "Deployment", w


def _pod(name: str, ns: str, node: str, minute: float) -> dict:
    kind, wl = _wl(name, node)
    if kind == "Static":
        owner = {"kind": "Node", "name": node, "controller": True}
    elif kind == "Deployment":
        owner = {"kind": "ReplicaSet", "name": name.rsplit("-", 1)[0], "controller": True}
    else:
        owner = {"kind": kind, "name": wl, "controller": True}
    labels = LABEL.get(wl, {"app": wl}) | ({"pod-template-hash": name.rsplit("-", 2)[-2]} if kind == "Deployment" else {})
    state, last, restarts, ready = {"running": {"startedAt": iso(BOOT)}}, {}, 0, True
    if wl == "worker" and minute >= 30:
        restarts = 1 + int((minute - 30) / 3)
        state = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 2m40s restarting failed container"}}
        last = {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
        ready = False
    res = {"requests": {"cpu": "50m", "memory": "64Mi"}}
    if wl in LIMITS:
        res["limits"] = {"memory": LIMITS[wl]} | ({"cpu": "500m"} if wl in ("nobetci", "web") else {})
    return {
        "metadata": {"name": name, "namespace": ns, "labels": labels, "ownerReferences": [owner],
                     "creationTimestamp": iso(BOOT + (3600 if ns == "demo" else 0))},
        "spec": {"nodeName": node, "containers": [{"name": wl, "image": IMAGE.get(wl, "busybox:1.36"), "resources": res}],
                 "hostNetwork": kind in ("Static", "DaemonSet")},
        "status": {
            "phase": "Running", "podIP": "172.18.0.2" if kind in ("Static", "DaemonSet") else f"10.244.0.{11 + abs(hash(name)) % 200}",
            "startTime": iso(BOOT), "qosClass": "Burstable",
            "conditions": [{"type": t, "status": "True" if (ready or t in ("PodScheduled", "Initialized")) else "False"}
                           for t in ("PodScheduled", "Initialized", "ContainersReady", "Ready")],
            "containerStatuses": [{"name": wl, "image": IMAGE.get(wl, "busybox:1.36"), "ready": ready,
                                   "restartCount": restarts, "state": state, "lastState": last}],
        },
    }


def _svc(ns, name, selector, ports, type_="ClusterIP", ip=None):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"type": type_, "clusterIP": ip or f"10.96.{len(name)}.{10 + len(ns)}", "selector": selector,
                     "ports": [dict(p, protocol="TCP") for p in ports]}}


def _events(node: str, minute: float) -> list[dict]:
    now = time.time()
    ev = []

    def add(ago, type_, reason, kind, name, ns, msg, src, count=1):
        ev.append({"type": type_, "reason": reason, "involvedObject": {"kind": kind, "name": name, "namespace": ns},
                   "message": msg, "lastTimestamp": iso(now - ago), "count": count, "source": {"component": src}})

    add(3000, "Normal", "ScalingReplicaSet", "Deployment", "web", "demo", "Scaled up replica set web-7d9f8b6c5d to 2", "deployment-controller")
    add(2990, "Normal", "SuccessfulCreate", "ReplicaSet", "web-7d9f8b6c5d", "demo", "Created pod: web-7d9f8b6c5d-r8t2v", "replicaset-controller")
    add(2985, "Normal", "Scheduled", "Pod", "web-7d9f8b6c5d-r8t2v", "demo", f"Successfully assigned demo/web-7d9f8b6c5d-r8t2v to {node}", "default-scheduler")
    add(2980, "Normal", "Pulled", "Pod", "web-7d9f8b6c5d-r8t2v", "demo", 'Container image "nginx:1.27" already present on machine', "kubelet")
    add(2978, "Normal", "Created", "Pod", "web-7d9f8b6c5d-r8t2v", "demo", "Created container web", "kubelet")
    add(2977, "Normal", "Started", "Pod", "web-7d9f8b6c5d-r8t2v", "demo", "Started container web", "kubelet")
    if minute >= 30:
        n = 1 + int((minute - 30) / 3)
        add(240, "Warning", "OOMKilling", "Node", node, "", "Memory cgroup out of memory: Killed process 4121 (worker)", "kernel-monitor")
        add(120, "Normal", "Pulled", "Pod", "worker-5c7d8f9b6-m4n5p", "demo", 'Container image "ghcr.io/ornek/worker:2.4.1" already present on machine', "kubelet", n)
        add(30, "Warning", "BackOff", "Pod", "worker-5c7d8f9b6-m4n5p", "demo", "Back-off restarting failed container worker in pod worker-5c7d8f9b6-m4n5p", "kubelet", 2 * n)
    return ev


def respond(path: str, minute: float, node: str, pods_ns: dict[str, str]):
    """Kubernetes API yoluna sahte cevap; tanınmayan yol için None (404)."""
    pods = [_pod(p, ns, node, minute) for p, ns in pods_ns.items()]
    deps = sorted({(p["metadata"]["namespace"], _wl(p["metadata"]["name"], node)[1], p["metadata"]["ownerReferences"][0]["name"])
                   for p in pods if _wl(p["metadata"]["name"], node)[0] == "Deployment"})

    def count(ns, wl):
        return [p for p in pods if p["metadata"]["namespace"] == ns and _wl(p["metadata"]["name"], node)[1] == wl]

    def ready(ps):
        return sum(1 for p in ps if p["status"]["containerStatuses"][0]["ready"])

    lists = {
        "/api/v1/nodes": [{
            "metadata": {"name": node, "creationTimestamp": iso(BOOT),
                         "labels": {"node-role.kubernetes.io/control-plane": "", "kubernetes.io/hostname": node}},
            "spec": {},
            "status": {"capacity": {"cpu": "4", "memory": "8148124Ki", "pods": "110"},
                       "allocatable": {"cpu": "4", "memory": "8148124Ki", "pods": "110"},
                       "conditions": [{"type": t, "status": "True" if t == "Ready" else "False"}
                                      for t in ("MemoryPressure", "DiskPressure", "PIDPressure", "Ready")],
                       "addresses": [{"type": "InternalIP", "address": "172.18.0.2"}],
                       "nodeInfo": {"kubeletVersion": "v1.31.0", "containerRuntimeVersion": "containerd://1.7.18",
                                    "osImage": "Debian GNU/Linux 12 (bookworm)", "kernelVersion": "6.8.0-45-generic",
                                    "architecture": "amd64"}}}],
        "/api/v1/pods": pods,
        "/api/v1/namespaces": [{"metadata": {"name": n}} for n in
                               ("default", "kube-system", "kube-public", "kube-node-lease", "local-path-storage",
                                "monitoring", "nobetci", "demo", "alarm-ornek")],
        "/api/v1/services": [
            _svc("default", "kubernetes", {}, [{"port": 443, "targetPort": 6443, "name": "https"}], ip="10.96.0.1"),
            _svc("kube-system", "kube-dns", {"k8s-app": "kube-dns"}, [{"port": 53, "targetPort": 53, "name": "dns"}], ip="10.96.0.10"),
            _svc("demo", "web", {"app": "web"}, [{"port": 80, "targetPort": 8080, "nodePort": 30080}], "NodePort"),
            _svc("demo", "api", {"app": "api"}, [{"port": 8080, "targetPort": 8080}]),
            _svc("demo", "cache", {"app": "cache"}, [{"port": 6379, "targetPort": 6379}], ip="None"),
            _svc("monitoring", "prometheus-server", {"app": "prometheus-server"}, [{"port": 80, "targetPort": 9090}]),
            _svc("monitoring", "kube-state-metrics", {"app": "kube-state-metrics"}, [{"port": 8080, "targetPort": 8080}]),
            _svc("nobetci", "nobetci", {"app": "nobetci"}, [{"port": 80, "targetPort": 8080}]),
            _svc("alarm-ornek", "alarm-api", {"app": "alarm-api"}, [{"port": 80, "targetPort": 8080}]),
            _svc("alarm-ornek", "role-gecidi", {"app": "role-gecidi"}, [{"port": 80, "targetPort": 8080}]),
        ],
        "/apis/apps/v1/deployments": [{
            "metadata": {"namespace": ns, "name": wl, "generation": 3},
            "spec": {"replicas": len(count(ns, wl)), "strategy": {"type": "RollingUpdate"}},
            "status": {"readyReplicas": ready(count(ns, wl)), "updatedReplicas": len(count(ns, wl)),
                       "unavailableReplicas": len(count(ns, wl)) - ready(count(ns, wl))}} for ns, wl in sorted({(a, b) for a, b, _ in deps})],
        "/apis/apps/v1/replicasets": [{
            "metadata": {"namespace": ns, "name": rs, "ownerReferences": [{"kind": "Deployment", "name": wl, "controller": True}]},
            "spec": {"replicas": len(count(ns, wl))}, "status": {"replicas": len(count(ns, wl)), "readyReplicas": ready(count(ns, wl))}}
            for ns, wl, rs in deps],
        "/apis/apps/v1/statefulsets": [{"metadata": {"namespace": "demo", "name": "cache", "generation": 1},
                                        "spec": {"replicas": 1, "updateStrategy": {"type": "RollingUpdate"}},
                                        "status": {"readyReplicas": 1, "updatedReplicas": 1}}],
        "/apis/apps/v1/daemonsets": [{"metadata": {"namespace": "kube-system", "name": d, "generation": 1},
                                      "spec": {"updateStrategy": {"type": "RollingUpdate"}},
                                      "status": {"desiredNumberScheduled": 1, "numberReady": 1, "updatedNumberScheduled": 1}}
                                     for d in DAEMON],
        "/api/v1/events": _events(node, minute),
    }
    if path in lists:
        return {"items": lists[path]}
    parts = path.strip("/").split("/")
    # /api/v1/namespaces/<ns>/events · /pods/<pod> · /pods/<pod>/log
    if len(parts) == 5 and parts[4] == "events":
        return {"items": [e for e in lists["/api/v1/events"] if e["involvedObject"]["namespace"] == parts[3]]}
    if len(parts) >= 6 and parts[4] == "pods":
        pod = next((p for p in pods if p["metadata"]["name"] == parts[5]), None)
        if pod is None:
            return None
        if len(parts) == 7 and parts[6] == "log":
            return "işçi başladı\nkuyruk okunuyor: 1200 iş\nbellek: 240MiB\n" if "worker" in parts[5] else "hazır\n"
        return pod
    return None
