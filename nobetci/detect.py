"""Tespit algoritmaları. Saf fonksiyonlar; G/Ç yok, yalnızca standart kütüphane.

Üç parça:
- detect_jump: yakın geçmişe göre ani artış/düşüş (medyan + MAD ile sağlam z-skoru)
- HourProfile: günlerin aynı saatindeki normal aralık (her gün tekrarlayan işleri ayıklar)
- detect_fill: doğrusal eğilimle "limit ne zaman dolacak" tahmini
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


def median_mad(xs: list[float]) -> tuple[float, float]:
    med = statistics.median(xs)
    return med, statistics.median([abs(x - med) for x in xs])


@dataclass
class Jump:
    direction: str  # up | down
    value: float
    baseline: float
    score: float


def baseline(window: list[float], *, min_abs: float, z: float, rel_floor: float = 0.05) -> tuple[float, float]:
    """Pencerenin "normal"i ve ölçeği. Ölçek MAD'den gelir; düz serilerde sıfıra inmesin diye
    medyanın %5'i ve min_abs/z ile tabanlanır. Eşik zarfı: normal ± z * ölçek."""
    med, mad = median_mad(window)
    return med, max(1.4826 * mad, rel_floor * abs(med), min_abs / z)


def detect_jump(values: list[float], *, min_abs: float, z: float = 5.0, persist: int = 2,
                warmup: int = 20, window: int = 60, rel_floor: float = 0.05) -> Jump | None:
    """Son `persist` noktanın tamamı, önceki `window` noktanın normalinden
    z * ölçek kadar uzaktaysa sıçrama döner."""
    if len(values) < warmup + persist:
        return None
    recent = values[-persist:]
    med, scale = baseline(values[-(window + persist):-persist], min_abs=min_abs, z=z, rel_floor=rel_floor)
    zs = [(v - med) / scale for v in recent]
    if min(zs) > z:
        return Jump("up", recent[-1], med, min(zs))
    # Düşüş yalnızca kayda değer bir seviyeden en az yarıya inildiyse anlamlı.
    if max(zs) < -z and max(recent) <= 0.5 * med:
        return Jump("down", recent[-1], med, -max(zs))
    return None


class HourProfile:
    """Günün her saati için geçmiş günlerdeki (min, max) aralığının medyanı.

    Gün başına tek değer alıp günler arasında medyan aldığı için tek bir günde
    yaşanmış anomali profili bozmaz; her gün aynı saatte çalışan iş ise profile girer."""

    def __init__(self) -> None:
        self._d: dict[int, dict[int, list[float]]] = {}

    def add(self, ts: float, v: float) -> None:
        day = self._d.setdefault(int(ts // 3600) % 24, {})
        mm = day.get(int(ts // 86400))
        if mm is None:
            day[int(ts // 86400)] = [v, v]
        else:
            mm[0] = min(mm[0], v)
            mm[1] = max(mm[1], v)

    def band(self, ts: float, min_days: int = 3) -> tuple[float, float] | None:
        days = self._d.get(int(ts // 3600) % 24)
        if not days or len(days) < min_days:
            return None
        return (statistics.median(m[0] for m in days.values()),
                statistics.median(m[1] for m in days.values()))

    def expected(self, jump: Jump, ts: float, slack: float = 0.25) -> bool:
        """Sıçrama bu saat için olağan aralıkta mı?"""
        band = self.band(ts)
        if band is None:
            return False
        lo, hi = band
        return jump.value <= hi * (1 + slack) if jump.direction == "up" else jump.value >= lo * (1 - slack)


def linear_fit(ts: list[float], vs: list[float]) -> tuple[float, float]:
    """En küçük kareler: (eğim, R²)."""
    n = len(ts)
    mt, mv = sum(ts) / n, sum(vs) / n
    stt = sum((t - mt) ** 2 for t in ts)
    svv = sum((v - mv) ** 2 for v in vs)
    if stt == 0 or svv == 0:
        return 0.0, 0.0
    stv = sum((t - mt) * (v - mv) for t, v in zip(ts, vs))
    return stv / stt, (stv * stv) / (stt * svv)


def detect_fill(ts: list[float], vs: list[float], limit: float, *, horizon: float,
                min_r2: float = 0.9, min_growth: float = 0.05) -> float | None:
    """Seri istikrarlı biçimde limite doğru büyüyorsa dolmaya kalan süreyi (sn) döner.

    Artış yeni başladıysa uzun pencere "düz + rampa" görür ve doğruya uymaz; bu yüzden
    kısadan uzuna birkaç pencere denenir, ilk uyan kullanılır."""
    if limit <= 0:
        return None
    for n in (20, 40, 60):
        if len(vs) < n:
            break
        slope, r2 = linear_fit(ts[-n:], vs[-n:])
        if slope > 0 and r2 >= min_r2 and vs[-1] - vs[-n] >= min_growth * limit:
            eta = (limit - vs[-1]) / slope
            return max(eta, 0.0) if eta <= horizon else None
    return None
