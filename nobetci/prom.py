"""Prometheus HTTP API istemcisi (yalnızca standart kütüphane)."""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request


class PromError(Exception):
    pass


class Prom:
    def __init__(self, url: str, timeout: float = 20):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _call(self, path: str, params: dict) -> list:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(self.url + path, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.load(r)
        except (OSError, ValueError) as e:
            raise PromError(f"{path}: {e}") from e
        if body.get("status") != "success":
            raise PromError(f"{path}: {body.get('error', 'bilinmeyen hata')}")
        return body["data"]["result"]

    def instant(self, query: str, ts: float) -> list[tuple[dict, float]]:
        out = []
        for r in self._call("/api/v1/query", {"query": query, "time": f"{ts:.3f}"}):
            v = float(r["value"][1])
            if math.isfinite(v):
                out.append((r["metric"], v))
        return out

    def range(self, query: str, start: float, end: float, step: int) -> list[tuple[dict, list[tuple[float, float]]]]:
        params = {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": str(step)}
        out = []
        for r in self._call("/api/v1/query_range", params):
            pts = [(float(t), float(v)) for t, v in r["values"]]
            out.append((r["metric"], [p for p in pts if math.isfinite(p[1])]))
        return out
