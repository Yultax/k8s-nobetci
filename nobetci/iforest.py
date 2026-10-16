"""Isolation Forest: etiket istemeyen (gözetimsiz) anomali tespiti. Yalnızca standart kütüphane.

Fikir: veriyi rastgele eksenlerde rastgele yerlerden bölmeye devam et. Kalabalığın içindeki bir
nokta ancak çok bölmeyle tek başına kalır; tuhaf bir nokta birkaç bölmede yalnızlaşır. Yüzlerce
rastgele ağaçta bir noktanın ortalama "yalnızlaşma derinliği" ne kadar kısaysa o kadar anormaldir.

Liu, Ting, Zhou (2008), "Isolation Forest". Skor = 2^(−ortalama derinlik / c(ψ)):
~0.5 sıradan, 1'e yaklaştıkça tuhaf.

Nöbetçide her iş yükünün kendi ormanı vardır; girdiler pod'un aynı andaki CPU, bellek, gelen ve
giden ağ değerleridir (her biri o sinyalin "en küçük anlamlı değişim" birimiyle bölünmüş). Eğitim
verisine ±yarım birimlik titreşim eklenir: düz bir seride önemsiz bir oynama tuhaf görünmesin.

Günün saati de iki girdi olarak verilir: saat bir çemberin üzerinde bir nokta (23:00 ile 00:00 yan
yana). Böylece "gündüz olağan olan trafik gece 03:00'te" de tuhaf bulunur.
"""
from __future__ import annotations

import math
import random
import statistics

EULER = 0.5772156649
HOUR_RADIUS = 4.0  # saat çemberinin yarıçapı (birim); komşu iki saat arası ~1 birim


def hour_coords(hour: float) -> list[float]:
    a = 2 * math.pi * hour / 24
    return [HOUR_RADIUS * math.cos(a), HOUR_RADIUS * math.sin(a)]


def c(n: int) -> float:
    """n noktalık bir ağaçta başarısız aramanın ortalama derinliği (normalleştirme sabiti)."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2 * (math.log(n - 1) + EULER) - 2 * (n - 1) / n


class IsolationForest:
    def __init__(self, trees: int = 100, sample: int = 256, seed: int = 0):
        self.n_trees = trees
        self.sample = sample
        self.seed = seed
        self.trees: list = []
        self.psi = 0
        self.dims = 0

    def fit(self, rows: list[list[float]], jitter: float = 0.5) -> "IsolationForest":
        rng = random.Random(self.seed)
        self.dims = len(rows[0])
        self.psi = min(self.sample, len(rows))
        limit = math.ceil(math.log2(max(self.psi, 2)))
        self.trees = []
        for _ in range(self.n_trees):
            sub = [[v + rng.uniform(-jitter, jitter) for v in r] for r in rng.sample(rows, self.psi)]
            self.trees.append(self._grow(sub, 0, limit, rng))
        return self

    def _grow(self, rows: list[list[float]], depth: int, limit: int, rng: random.Random):
        """Ağaç düğümü: yaprak (nokta sayısı,) ya da (eksen, eşik, alt, üst, sol, sağ).
        alt/üst, düğümdeki verinin o eksendeki aralığıdır; skorlamada kullanılır."""
        if depth >= limit or len(rows) <= 1:
            return (len(rows),)
        spans = []
        for j in range(self.dims):
            lo = min(r[j] for r in rows)
            hi = max(r[j] for r in rows)
            if hi > lo:
                spans.append((j, lo, hi))
        if not spans:
            return (len(rows),)
        j, lo, hi = rng.choice(spans)
        s = rng.uniform(lo, hi)
        left = [r for r in rows if r[j] < s]
        right = [r for r in rows if r[j] >= s]
        return (j, s, lo, hi, self._grow(left, depth + 1, limit, rng), self._grow(right, depth + 1, limit, rng))

    @staticmethod
    def path(tree, x: list[float]) -> float:
        """Noktanın beklenen yalnızlaşma derinliği.

        Skorlanan nokta ağacı kuran veride yoktu. Bir düğümün verisi bir eksende [alt, üst] aralığındaysa
        ve nokta bu aralığın `boşluk` kadar dışındaysa, nokta veride olsaydı o düğümün bölmesi onu
        boşluk / (üst − alt + boşluk) olasılıkla hemen ayırırdı. Bu olasılık beklenen derinliğe katılır;
        böylece aralığın çok dışındaki nokta, aralığın ucundaki sıradan noktayla aynı skoru almaz."""
        depth, rest, total = 0, 1.0, 0.0
        while len(tree) == 6:
            j, s, lo, hi, left, right = tree
            v = x[j]
            gap = lo - v if v < lo else v - hi if v > hi else 0.0
            if gap > 0:
                p = gap / (hi - lo + gap)
                total += rest * p * (depth + 1)
                rest *= 1 - p
            tree = left if v < s else right
            depth += 1
        return total + rest * (depth + c(tree[0]))

    def score(self, x: list[float]) -> float:
        mean = sum(self.path(t, x) for t in self.trees) / len(self.trees)
        return 2 ** (-mean / c(self.psi))


def quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


class Model:
    """Bir iş yükünün eğitilmiş ormanı ve ekranda gösterilecek özeti.

    hours verilirse her satırın günün saati (0–24, ondalıklı) de girdi olur. accept, kullanıcının
    "yanlış alarm" dediği anlar: eşik, bu anların çoğunu alarm saymayacak kadar yükseltilir (en fazla +0.15)."""

    def __init__(self, rows: list[list[float]], units: list[float], *, trees: int, sample: int, seed: int,
                 q: float, trained_at: float, pods: int, hours: list[float] | None = None,
                 accept: list[tuple[list[float], float | None]] = (), floor: float = 0.55, margin: float = 0.03):
        rng = random.Random(seed)
        self.units = units
        self.uses_hour = hours is not None
        scaled = [self._x(r, h) for r, h in zip(rows, hours or [None] * len(rows))]
        self.forest = IsolationForest(trees, sample, seed).fit(scaled)
        check = rng.sample(scaled, min(600, len(scaled)))
        # Eşik modelin kendi eğitim verisinden: "gördüğüm normal noktaların en tuhaf %0.5'inden de tuhaf".
        self.base_threshold = self.threshold = max(floor, quantile([self.forest.score(r) for r in check], q) + margin)
        self.accepted = len(accept)
        if accept:
            seen = quantile([self.score(r, h) for r, h in accept], 0.9) + 0.02
            self.threshold = max(self.threshold, min(seen, self.threshold + 0.15))
        self.median, self.lo, self.hi = [], [], []
        for j in range(len(units)):
            col = sorted(r[j] for r in rows)
            self.median.append(statistics.median(col))
            self.lo.append(quantile(col, 0.01))
            self.hi.append(quantile(col, 0.99))
        # "Neden?" açıklaması için saat başına aralık (komşu saatlerle birlikte; az veri varsa genel aralık).
        self.by_hour: dict[int, tuple[list[float], list[float]]] = {}
        if hours is not None:
            buckets: dict[int, list[list[float]]] = {}
            for r, h in zip(rows, hours):
                for k in (int(h) - 1, int(h), int(h) + 1):
                    buckets.setdefault(k % 24, []).append(r)
            for k, rs in buckets.items():
                if len(rs) >= 30:
                    cols = [sorted(r[j] for r in rs) for j in range(len(units))]
                    self.by_hour[k] = ([quantile(c, 0.01) for c in cols], [quantile(c, 0.99) for c in cols])
        self.samples = len(rows)
        self.pods = pods
        self.trained_at = trained_at
        self.preview = [[round(v, 6) for v in r] for r in rng.sample(rows, min(400, len(rows)))]

    def _x(self, x: list[float], hour: float | None) -> list[float]:
        out = [v / u for v, u in zip(x, self.units)]
        return out + hour_coords(hour) if self.uses_hour else out

    def score(self, x: list[float], hour: float | None = None) -> float:
        return self.forest.score(self._x(x, hour if hour is not None else 12.0))

    def range_at(self, hour: float | None) -> tuple[list[float], list[float], bool]:
        """(alt, üst, saate özgü mü): ölçümlerin o saatteki olağan aralığı, yoksa genel aralık."""
        if hour is not None and int(hour) % 24 in self.by_hour:
            lo, hi = self.by_hour[int(hour) % 24]
            return lo, hi, True
        return self.lo, self.hi, False

    def deviations(self, x: list[float], hour: float | None = None) -> list[float]:
        """"Neden?" sorusu için: her ölçüm tek başına eğitim verisinin %1–%99 aralığının ne kadar dışında
        (saat verilirse o saatin aralığı; en küçük anlamlı değişim birimiyle; içerideyse 0, altındaysa eksi)."""
        lo, hi, _ = self.range_at(hour)
        return [(v - b) / u if v > b else (v - a) / u if v < a else 0.0
                for v, a, b, u in zip(x, lo, hi, self.units)]
