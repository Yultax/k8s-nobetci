"""Kubernetes API'sinden salt okunur bağlam toplama (pod durumu, olaylar, loglar)."""
from __future__ import annotations

import json
import os
import ssl
import urllib.parse
import urllib.request

_SA = "/var/run/secrets/kubernetes.io/serviceaccount"


class Kube:
    def __init__(self, url: str, token: str, ca: str | None):
        self.url = url
        self.token = token
        self.ctx = ssl.create_default_context(cafile=ca) if ca else None

    @classmethod
    def in_cluster(cls) -> "Kube | None":
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        if not host or not os.path.exists(f"{_SA}/token"):
            return None
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        with open(f"{_SA}/token") as f:
            return cls(f"https://{host}:{port}", f.read().strip(), f"{_SA}/ca.crt")

    def _get(self, path: str, params: dict | None = None, text: bool = False):
        url = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        try:
            with urllib.request.urlopen(req, timeout=10, context=self.ctx) as r:
                body = r.read().decode("utf-8", "replace")
        except OSError:
            return None
        return body if text else json.loads(body)

    def pod(self, ns: str, name: str) -> dict | None:
        return self._get(f"/api/v1/namespaces/{ns}/pods/{name}")

    def events(self, ns: str, prefix: str, limit: int = 8) -> list[dict]:
        """Adı `prefix` ile başlayan nesnelerin (pod, replicaset, deployment) son olayları."""
        data = self._get(f"/api/v1/namespaces/{ns}/events", {"limit": 500}) if ns else \
            self._get("/api/v1/events", {"fieldSelector": f"involvedObject.kind=Node,involvedObject.name={prefix}"})
        items = [e for e in (data or {}).get("items", []) if e.get("involvedObject", {}).get("name", "").startswith(prefix)]
        when = lambda e: e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp") or ""
        return sorted(items, key=when)[-limit:]

    # Keşif ekranının küme haritası için listelenen nesneler (hepsi salt okunur `list`).
    LISTS = {
        "nodes": "/api/v1/nodes", "pods": "/api/v1/pods", "services": "/api/v1/services",
        "namespaces": "/api/v1/namespaces", "events": "/api/v1/events",
        "deployments": "/apis/apps/v1/deployments", "replicasets": "/apis/apps/v1/replicasets",
        "statefulsets": "/apis/apps/v1/statefulsets", "daemonsets": "/apis/apps/v1/daemonsets",
    }

    def snapshot(self) -> dict[str, list | None]:
        """Kümenin anlık hâli. Yetki ya da erişim yoksa ilgili liste None olur."""
        out: dict[str, list | None] = {}
        for key, path in self.LISTS.items():
            data = self._get(path, {"limit": 500} if key == "events" else None)
            out[key] = data.get("items", []) if isinstance(data, dict) else None
        return out

    def logs(self, ns: str, pod: str, container: str = "", previous: bool = False, tail: int = 15) -> list[str]:
        params = {"tailLines": tail, "previous": str(previous).lower()}
        if container:
            params["container"] = container
        text = self._get(f"/api/v1/namespaces/{ns}/pods/{pod}/log", params, text=True)
        return [line[:200] for line in (text or "").splitlines() if line.strip()]
