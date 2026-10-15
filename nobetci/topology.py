"""Keşif ekranı için küme haritası: Kubernetes nesneleri + ölçümler + açık olaylar tek yapıda.

Saf fonksiyonlar; G/Ç yok. Kubernetes API'ye erişim yoksa harita yalnızca Prometheus'tan
bilinen pod ve node'larla kurulur (service, sahiplik ve durum bilgisi olmadan)."""
from __future__ import annotations

import re

from .signals import workload_of

_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "m": 1e-3}
_QTY = re.compile(r"^([0-9.eE+-]+)([a-zA-Z]*)$")


def qty(s) -> float | None:
    """Kubernetes miktarı: "250m" → 0.25, "512Mi" → 536870912."""
    if s is None:
        return None
    m = _QTY.match(str(s))
    if not m or m.group(2) not in ("", *_UNITS):
        return None
    try:
        return float(m.group(1)) * _UNITS.get(m.group(2), 1)
    except ValueError:
        return None


def _owner(meta: dict) -> tuple[str, str]:
    refs = meta.get("ownerReferences") or []
    ref = next((r for r in refs if r.get("controller")), refs[0] if refs else None)
    return (ref["kind"], ref["name"]) if ref else ("", "")


def _resources(spec: dict) -> dict:
    out = {"req_cpu": 0.0, "req_mem": 0.0, "lim_cpu": 0.0, "lim_mem": 0.0}
    for c in spec.get("containers", []):
        r = c.get("resources", {})
        for kind, key in (("requests", "req"), ("limits", "lim")):
            for res in ("cpu", "memory"):
                v = qty(r.get(kind, {}).get(res))
                if v:
                    out[f"{key}_{'mem' if res == 'memory' else 'cpu'}"] += v
    return {k: (v or None) for k, v in out.items()}


def _container(cs: dict) -> dict:
    state = cs.get("state", {})
    kind = next(iter(state), "")
    last = cs.get("lastState", {}).get("terminated", {})
    return {"name": cs.get("name", ""), "image": cs.get("image", ""), "ready": bool(cs.get("ready")),
            "restarts": cs.get("restartCount", 0), "state": kind, "reason": state.get(kind, {}).get("reason", ""),
            "last": last.get("reason", ""), "last_code": last.get("exitCode")}


def _status(pod: dict, phase: str, containers: list[dict]) -> str:
    """Pod'un ekrandaki tek kelimelik durumu (kubectl get pods'un STATUS sütununa benzer)."""
    if pod.get("metadata", {}).get("deletionTimestamp"):
        return "Terminating"
    for c in containers:
        if c["state"] == "waiting" and c["reason"] not in ("", "ContainerCreating", "PodInitializing"):
            return c["reason"]
    for c in containers:
        if c["state"] == "terminated" and c["reason"] and phase not in ("Succeeded", "Failed"):
            return c["reason"]
    if phase == "Running" and containers and not all(c["ready"] for c in containers):
        return "NotReady"
    if phase == "Pending" and pod.get("spec", {}).get("nodeName"):
        return "ContainerCreating"
    return phase or "Unknown"


def _sel_match(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _incident_index(open_incidents: list[dict]) -> tuple[dict, dict, dict]:
    by_pod, by_wl, by_node = {}, {}, {}

    def put(d, key, inc):
        if key not in d or d[key]["severity"] < inc["severity"]:
            d[key] = {"id": inc["id"], "severity": inc["severity"], "title": inc["title"]}

    for inc in open_incidents:
        if inc["scope"] == "node":
            put(by_node, inc["workload"], inc)
            continue
        put(by_wl, (inc["namespace"], inc["workload"]), inc)
        for f in inc["findings"].values():
            if f.get("pod"):
                put(by_pod, (inc["namespace"], f["pod"]), inc)
    return by_pod, by_wl, by_node


def _metrics(view: dict) -> tuple[dict, dict]:
    pods = {(r["ns"], r["pod"]): r for r in view.get("pods", [])}
    nodes: dict[str, dict] = {}
    for c in view.get("nodes", []):
        n = nodes.setdefault(c["entity"], {})
        key = "cpu" if c["signal"] == "node_cpu" else "mem"
        n[key] = c["vs"][-1] if c["vs"] else None
        n[key + "_hist"] = c["vs"][-30:]
    return pods, nodes


def _pod_metrics(row: dict | None) -> dict:
    if not row:
        return {}
    keys = ("pod_cpu", "pod_mem", "pod_net_rx", "pod_net_tx")
    out = {k.split("_", 1)[1]: row.get(k) for k in keys} | {k.split("_", 1)[1] + "_z": row.get(k + "_z") for k in keys}
    out["mem_limit"] = row.get("limit")
    return out


def build(snap: dict | None, view: dict) -> dict:
    """`snap`: Kube.snapshot() çıktısı (ya da None); `view`: App.state() çıktısı."""
    snap = snap or {}
    metrics, node_metrics = _metrics(view)
    inc_pod, inc_wl, inc_node = _incident_index(view.get("open", []))
    missing = sorted(k for k, v in snap.items() if v is None) if snap else []

    # --- sahiplik zinciri: Pod → ReplicaSet → Deployment ---------------------
    rs_owner = {}
    for rs in snap.get("replicasets") or []:
        m = rs["metadata"]
        rs_owner[(m["namespace"], m["name"])] = _owner(m)

    # --- pod'lar ---------------------------------------------------------------
    pods, seen = [], set()
    for p in snap.get("pods") or []:
        meta, spec, st = p["metadata"], p.get("spec", {}), p.get("status", {})
        ns, name = meta["namespace"], meta["name"]
        okind, oname = _owner(meta)
        wkind, wname = okind, oname
        if okind == "ReplicaSet":
            up = rs_owner.get((ns, oname))
            wkind, wname = up if up and up[0] else ("Deployment", workload_of(name))
        elif okind == "Node":  # kubelet'in dosyadan başlattığı statik pod (etcd, kube-apiserver...)
            wkind, wname = "Static", name
        elif not okind:
            wkind, wname = "Pod", name
        containers = [_container(c) for c in st.get("containerStatuses", [])]
        if not containers:
            containers = [{"name": c["name"], "image": c.get("image", ""), "ready": False, "restarts": 0,
                           "state": "waiting", "reason": "", "last": "", "last_code": None} for c in spec.get("containers", [])]
        phase = st.get("phase", "")
        seen.add((ns, name))
        pods.append({
            "ns": ns, "name": name, "node": spec.get("nodeName", ""), "phase": phase,
            "status": _status(p, phase, containers), "ip": st.get("podIP", ""), "host_net": bool(spec.get("hostNetwork")),
            "created": meta.get("creationTimestamp", ""), "started": st.get("startTime", ""),
            "owner": {"kind": okind, "name": oname}, "workload": {"kind": wkind, "name": wname},
            "labels": dict(list((meta.get("labels") or {}).items())[:10]), "qos": st.get("qosClass", ""),
            "conditions": {c["type"]: c.get("status") == "True" for c in st.get("conditions", [])},
            "containers": containers, "restarts": sum(c["restarts"] for c in containers),
            "ready": bool(containers) and all(c["ready"] for c in containers),
            "_labels": meta.get("labels") or {},
            **_resources(spec), **_pod_metrics(metrics.get((ns, name))),
            "incident": inc_pod.get((ns, name)) or inc_wl.get((ns, wname)) or inc_wl.get((ns, workload_of(name))),
        })
    if not snap.get("pods"):  # Kubernetes API yok: Prometheus'ta görülen pod'lar
        for (ns, name), row in metrics.items():
            if (ns, name) in seen:
                continue
            pods.append({"ns": ns, "name": name, "node": "", "phase": "", "status": "", "ip": "", "host_net": False,
                         "created": "", "started": "", "owner": {"kind": "", "name": ""},
                         "workload": {"kind": "", "name": workload_of(name)}, "labels": {}, "qos": "", "conditions": {},
                         "containers": [], "restarts": 0, "ready": True, "_labels": {}, **_pod_metrics(row),
                         "incident": inc_pod.get((ns, name)) or inc_wl.get((ns, workload_of(name)))})

    # --- node'lar ----------------------------------------------------------------
    nodes = []
    for n in snap.get("nodes") or []:
        meta, st = n["metadata"], n.get("status", {})
        conds = {c["type"]: c.get("status") == "True" for c in st.get("conditions", [])}
        info = st.get("nodeInfo", {})
        nodes.append({
            "name": meta["name"], "ready": conds.get("Ready", False),
            "roles": sorted(k.split("/", 1)[1] for k in (meta.get("labels") or {}) if k.startswith("node-role.kubernetes.io/")),
            "pressure": sorted(k for k, v in conds.items() if k != "Ready" and v),
            "cpu_cap": qty(st.get("capacity", {}).get("cpu")), "mem_cap": qty(st.get("capacity", {}).get("memory")),
            "cpu_alloc": qty(st.get("allocatable", {}).get("cpu")), "mem_alloc": qty(st.get("allocatable", {}).get("memory")),
            "pods_cap": qty(st.get("allocatable", {}).get("pods")),
            "kubelet": info.get("kubeletVersion", ""), "runtime": info.get("containerRuntimeVersion", ""),
            "os": info.get("osImage", ""), "kernel": info.get("kernelVersion", ""), "arch": info.get("architecture", ""),
            "ip": next((a["address"] for a in st.get("addresses", []) if a.get("type") == "InternalIP"), ""),
            "taints": [f"{t.get('key')}:{t.get('effect')}" for t in (n.get("spec", {}).get("taints") or [])],
            "created": meta.get("creationTimestamp", ""),
        })
    if not nodes:
        names = sorted({p["node"] for p in pods if p["node"]} | set(node_metrics)) or ["node"]
        nodes = [{"name": nm, "ready": True, "roles": [], "pressure": [], "cpu_cap": None, "mem_cap": None, "cpu_alloc": None,
                  "mem_alloc": None, "pods_cap": None, "kubelet": "", "runtime": "", "os": "", "kernel": "", "arch": "",
                  "ip": "", "taints": [], "created": ""} for nm in names]
    # Ölçüm etiketindeki node adı (instance) her zaman node adıyla aynı olmayabilir; tek node'da eşleştir.
    unmatched = [k for k in node_metrics if k not in {n["name"] for n in nodes}]
    for n in nodes:
        m = node_metrics.get(n["name"]) or (node_metrics[unmatched[0]] if len(nodes) == 1 and len(unmatched) == 1 else {})
        n.update({"cpu": m.get("cpu"), "mem": m.get("mem"), "cpu_hist": m.get("cpu_hist", []), "mem_hist": m.get("mem_hist", []),
                  "incident": inc_node.get(n["name"])})
        for p in pods:  # API yoksa pod'lar tek node'a yerleştirilir
            if not p["node"] and not snap.get("pods") and len(nodes) == 1:
                p["node"] = n["name"]

    # --- service'ler: seçici etiketleri pod etiketleriyle eşleştirilir -----------
    services = []
    for s in snap.get("services") or []:
        meta, spec = s["metadata"], s.get("spec", {})
        sel = spec.get("selector") or {}
        matched = [p["name"] for p in pods if p["ns"] == meta["namespace"] and _sel_match(sel, p["_labels"])]
        services.append({
            "ns": meta["namespace"], "name": meta["name"], "type": spec.get("type", "ClusterIP"),
            "cluster_ip": spec.get("clusterIP", ""), "selector": sel, "pods": matched,
            "ports": [{"port": p.get("port"), "target": p.get("targetPort"), "node_port": p.get("nodePort"),
                       "protocol": p.get("protocol", "TCP"), "name": p.get("name", "")} for p in spec.get("ports", [])],
        })
    for p in pods:
        del p["_labels"]

    # --- iş yükleri ------------------------------------------------------------
    workloads = []
    for kind, key in (("Deployment", "deployments"), ("StatefulSet", "statefulsets"), ("DaemonSet", "daemonsets")):
        for w in snap.get(key) or []:
            meta, spec, st = w["metadata"], w.get("spec", {}), w.get("status", {})
            desired = st.get("desiredNumberScheduled") if kind == "DaemonSet" else spec.get("replicas", 1)
            ready = st.get("numberReady") if kind == "DaemonSet" else st.get("readyReplicas", 0)
            workloads.append({"kind": kind, "ns": meta["namespace"], "name": meta["name"], "desired": desired or 0,
                              "ready": ready or 0, "updated": st.get("updatedReplicas", st.get("updatedNumberScheduled", 0)) or 0,
                              "generation": meta.get("generation", 0),
                              "strategy": (spec.get("strategy") or spec.get("updateStrategy") or {}).get("type", "")})
    replicasets = []
    for rs in snap.get("replicasets") or []:
        meta, st = rs["metadata"], rs.get("status", {})
        if not rs.get("spec", {}).get("replicas") and not st.get("replicas"):
            continue  # eski sürümlerden kalan boş ReplicaSet'ler
        replicasets.append({"ns": meta["namespace"], "name": meta["name"], "owner": _owner(meta)[1],
                            "desired": rs.get("spec", {}).get("replicas", 0), "ready": st.get("readyReplicas", 0) or 0})

    # --- son Kubernetes olayları --------------------------------------------------
    def when(e):
        return e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp") or ""
    events = [{"time": when(e), "type": e.get("type", ""), "reason": e.get("reason", ""), "count": e.get("count") or 1,
               "kind": e.get("involvedObject", {}).get("kind", ""), "name": e.get("involvedObject", {}).get("name", ""),
               "ns": e.get("involvedObject", {}).get("namespace", ""), "message": (e.get("message") or "")[:220],
               "source": (e.get("source") or {}).get("component") or e.get("reportingComponent", "")}
              for e in sorted(snap.get("events") or [], key=when)[-40:]]

    namespaces = sorted({n["metadata"]["name"] for n in snap.get("namespaces") or []} | {p["ns"] for p in pods})
    return {
        "source": "kube" if snap.get("pods") is not None else "prometheus", "missing": missing,
        "cluster": view.get("cluster", ""), "version": view.get("version", ""), "now": view.get("now"),
        "cycle": view.get("cycle", 0), "interval": view.get("interval"), "ready": view.get("ready", False),
        "detector": view.get("detector", {}), "pipeline": view.get("pipeline", {}), "llm": view.get("llm", {}), "ml": view.get("ml", {}),
        "notify": view.get("notify", {}), "finding_counts": view.get("finding_counts", {}),
        "count_24h": view.get("count_24h", 0), "errors": view.get("errors", []),
        "incidents": [{"id": i["id"], "severity": i["severity"], "title": i["title"], "scope": i["scope"],
                       "namespace": i["namespace"], "workload": i["workload"], "opened": i["opened"],
                       "kinds": sorted({f["kind"] for f in i["findings"].values()}),
                       "summary": i.get("summary", {})} for i in view.get("open", [])],
        "namespaces": namespaces, "nodes": nodes, "pods": pods, "services": services,
        "workloads": workloads, "replicasets": replicasets, "events": events,
    }
