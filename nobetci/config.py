"""Ayarlar: hepsi NOBETCI_ önekli ortam değişkenlerinden okunur."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass


def _env(name: str, default, cast=str):
    raw = os.environ.get("NOBETCI_" + name)
    if raw is None or raw == "":
        return default
    return cast(raw)


@dataclass
class Config:
    cluster: str = "kubernetes"
    prom_url: str = "http://prometheus-server.monitoring.svc"
    interval: int = 60            # döngü aralığı (sn); tamponlardaki nokta aralığı da budur
    history_points: int = 180     # seri başına bellekte tutulan nokta (3 saat)
    z: float = 5.0                # sıçrama eşiği (sağlam z-skoru)
    persist: int = 2              # sıçramanın kaç nokta sürmesi gerektiği
    warmup: int = 20              # yeni seride tespit başlamadan önce beklenen nokta
    profile_days: int = 7         # saatlik profil için geriye bakılan gün
    profile_refresh: int = 6 * 3600
    fill_horizon: int = 6 * 3600  # "limit dolacak" uyarısı için ufuk (sn)
    quiet: int = 600              # bulgu gelmeyince olayın kapanma süresi (sn)
    ns_exclude: str = ""          # izlenmeyecek namespace'ler (regex)
    llm_url: str = ""             # OpenAI uyumlu uç, ör. http://ollama:11434
    llm_model: str = "gemma3:4b"
    llm_key: str = ""
    llm_timeout: int = 300
    llm_cooldown: int = 600       # aynı olay için yeniden yorum arası (sn)
    webhook_url: str = ""
    db: str = "/data/nobetci.db"
    port: int = 8080
    bind: str = "0.0.0.0"
    tz_offset: float = 3.0        # ekranda gösterilen saat dilimi (UTC+3)
    time_speed: float = 1.0       # yalnızca demo/test: sanal saat hızı

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        for name, default in vars(c).items():
            setattr(c, name, _env(name.upper(), default, type(default)))
        return c


class Clock:
    """Gerçek saat; time_speed > 1 ise demo için hızlandırılmış sanal saat."""

    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self._t0 = time.time()

    def now(self) -> float:
        return self._t0 + (time.time() - self._t0) * self.speed

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds) / self.speed)
