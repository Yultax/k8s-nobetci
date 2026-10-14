"""Olayların kalıcı kaydı (SQLite). Ölçüm saklamaz; ölçümün evi Prometheus'tur."""
from __future__ import annotations

import json
import os
import sqlite3

from .incident import Incident


class Store:
    def __init__(self, path: str):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.path = path
        except (OSError, sqlite3.Error):
            self.db = sqlite3.connect(":memory:", check_same_thread=False)
            self.path = ":memory:"
        self.db.execute("CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, closed REAL, data TEXT NOT NULL)")
        self.db.commit()

    def save(self, inc: Incident) -> None:
        data = inc.to_dict()
        del data["title"], data["entity"]
        self.db.execute("INSERT OR REPLACE INTO incidents (id, closed, data) VALUES (?, ?, ?)",
                        (inc.id, inc.closed, json.dumps(data, ensure_ascii=False)))
        self.db.commit()

    def load(self) -> tuple[list[Incident], list[Incident], int]:
        """(açık olaylar, son kapananlar, sıradaki id)"""
        def rows(where: str) -> list[Incident]:
            cur = self.db.execute(f"SELECT data FROM incidents WHERE {where}")
            return [Incident(**json.loads(r[0])) for r in cur]
        next_id = (self.db.execute("SELECT MAX(id) FROM incidents").fetchone()[0] or 0) + 1
        return rows("closed IS NULL"), rows("closed IS NOT NULL ORDER BY closed DESC LIMIT 50"), next_id

    def get(self, incident_id: int) -> Incident | None:
        row = self.db.execute("SELECT data FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return Incident(**json.loads(row[0])) if row else None

    def count_since(self, ts: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM incidents WHERE json_extract(data, '$.opened') >= ?", (ts,)).fetchone()[0]
