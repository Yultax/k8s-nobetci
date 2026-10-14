import unittest

from nobetci.topology import build, qty


def pod(name, ns="demo", labels=None, owner=("ReplicaSet", "web-7d9f8b6c5d"), ready=True, waiting=""):
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {"app": "web"},
                         "ownerReferences": [{"kind": owner[0], "name": owner[1], "controller": True}] if owner else []},
            "spec": {"nodeName": "n1", "containers": [{"name": "c", "resources": {"requests": {"cpu": "250m", "memory": "64Mi"},
                                                                                   "limits": {"memory": "512Mi"}}}]},
            "status": {"phase": "Running", "containerStatuses": [{"name": "c", "ready": ready, "restartCount": 2, "state": state}]}}


SNAP = {
    "nodes": [{"metadata": {"name": "n1", "labels": {"node-role.kubernetes.io/control-plane": ""}},
               "status": {"capacity": {"cpu": "4", "memory": "8Gi"}, "allocatable": {"cpu": "3900m", "memory": "7Gi"},
                          "conditions": [{"type": "Ready", "status": "True"}, {"type": "DiskPressure", "status": "True"}]}}],
    "pods": [pod("web-7d9f8b6c5d-x2k4p"), pod("web-7d9f8b6c5d-r8t2v", ready=False, waiting="CrashLoopBackOff"),
             pod("etcd-n1", ns="kube-system", labels={"component": "etcd"}, owner=("Node", "n1")), pod("tek", owner=None, labels={"app": "x"})],
    "services": [{"metadata": {"namespace": "demo", "name": "web"}, "spec": {"selector": {"app": "web"}, "ports": [{"port": 80}]}},
                 {"metadata": {"namespace": "demo", "name": "bos"}, "spec": {"selector": {"app": "yok"}}}],
    "replicasets": [{"metadata": {"namespace": "demo", "name": "web-7d9f8b6c5d", "ownerReferences": [{"kind": "Deployment", "name": "web"}]},
                     "spec": {"replicas": 2}, "status": {"readyReplicas": 1}}],
    "deployments": [{"metadata": {"namespace": "demo", "name": "web"}, "spec": {"replicas": 2}, "status": {"readyReplicas": 1}}],
    "statefulsets": [], "daemonsets": [], "namespaces": None, "events": [],
}
VIEW = {"pods": [{"ns": "demo", "pod": "web-7d9f8b6c5d-x2k4p", "pod_cpu": 0.2, "pod_cpu_z": 6.1, "pod_mem": 1e8, "limit": 5e8}],
        "nodes": [{"entity": "10.0.0.1:10250", "signal": "node_cpu", "vs": [1.0, 1.2]}],
        "open": [{"id": 7, "severity": 3, "title": "t", "scope": "pod", "namespace": "demo", "workload": "web", "opened": 0,
                  "findings": {"k": {"kind": "spike", "pod": "web-7d9f8b6c5d-x2k4p"}}}]}


class TopologyTest(unittest.TestCase):
    def setUp(self):
        self.t = build(SNAP, VIEW)
        self.pods = {p["name"]: p for p in self.t["pods"]}

    def test_qty(self):
        self.assertEqual(qty("250m"), 0.25)
        self.assertEqual(qty("512Mi"), 512 * 2**20)
        self.assertEqual(qty("4"), 4)
        self.assertIsNone(qty("abc"))

    def test_owner_chain(self):
        self.assertEqual(self.pods["web-7d9f8b6c5d-x2k4p"]["workload"], {"kind": "Deployment", "name": "web"})
        self.assertEqual(self.pods["etcd-n1"]["workload"]["kind"], "Static")
        self.assertEqual(self.pods["tek"]["workload"]["kind"], "Pod")

    def test_status_and_resources(self):
        bad = self.pods["web-7d9f8b6c5d-r8t2v"]
        self.assertEqual(bad["status"], "CrashLoopBackOff")
        self.assertFalse(bad["ready"])
        self.assertEqual(bad["req_cpu"], 0.25)
        self.assertEqual(bad["restarts"], 2)

    def test_metrics_and_incident(self):
        p = self.pods["web-7d9f8b6c5d-x2k4p"]
        self.assertEqual(p["cpu"], 0.2)
        self.assertEqual(p["cpu_z"], 6.1)
        self.assertEqual(p["incident"]["id"], 7)
        self.assertEqual(self.pods["web-7d9f8b6c5d-r8t2v"]["incident"]["id"], 7)  # aynı iş yükü

    def test_services_and_nodes(self):
        svc = {s["name"]: s for s in self.t["services"]}
        self.assertEqual(sorted(svc["web"]["pods"]), ["web-7d9f8b6c5d-r8t2v", "web-7d9f8b6c5d-x2k4p"])
        self.assertEqual(svc["bos"]["pods"], [])
        n = self.t["nodes"][0]
        self.assertEqual(n["pressure"], ["DiskPressure"])
        self.assertEqual(n["cpu_alloc"], 3.9)
        self.assertEqual(n["cpu"], 1.2)  # tek node: ölçüm etiketi farklı olsa da eşleşir
        self.assertEqual(self.t["missing"], ["namespaces"])
        self.assertNotIn("_labels", self.t["pods"][0])

    def test_without_kube_api(self):
        t = build(None, VIEW)
        self.assertEqual(t["source"], "prometheus")
        self.assertEqual([p["name"] for p in t["pods"]], ["web-7d9f8b6c5d-x2k4p"])
        self.assertEqual(t["pods"][0]["node"], t["nodes"][0]["name"])


if __name__ == "__main__":
    unittest.main()
