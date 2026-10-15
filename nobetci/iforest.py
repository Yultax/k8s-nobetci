"""Isolation Forest: etiket istemeyen (gözetimsiz) anomali tespiti. Yalnızca standart kütüphane.

Fikir: veriyi rastgele eksenlerde rastgele yerlerden bölmeye devam et. Kalabalığın içindeki bir
nokta ancak çok bölmeyle tek başına kalır; tuhaf bir nokta birkaç bölmede yalnızlaşır. Yüzlerce
rastgele ağaçta bir noktanın ortalama "yalnızlaşma derinliği" ne kadar kısaysa o kadar anormaldir.

Liu, Ting, Zhou (2008), "Isolation Forest". Skor = 2^(−ortalama derinlik / c(ψ)):
~0.5 sıradan, 1'e yaklaştıkça tuhaf.

Nöbetçide her iş yükünün kendi ormanı vardır; girdiler pod'un aynı andaki CPU, bellek, gelen ve
giden ağ değerleridir (her biri o sinyalin "en küçük anlamlı değişim" birimiyle bölünmüş). Eğitim
verisine ±yarım birimlik titreşim eklenir: düz bir seride önemsiz bir oynama tuhaf görünmesin.
"""
from __future__ import annotations

import math
import random
import statistics

EULER = 0.5772156649


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
    """Bir iş yükünün eğitilmiş ormanı ve ekranda gösterilecek özeti."""

    def __init__(self, rows: list[list[float]], units: list[float], *, trees: int, sample: int, seed: int,
                 q: float, trained_at: float, pods: int, floor: float = 0.55, margin: float = 0.03):
        rng = random.Random(seed)
        scaled = [[v / u for v, u in zip(r, units)] for r in rows]
        self.forest = IsolationForest(trees, sample, seed).fit(scaled)
        self.units = units
        check = rng.sample(scaled, min(600, len(scaled)))
        # Eşik modelin kendi eğitim verisinden: "gördüğüm normal noktaların en tuhaf %0.5'inden de tuhaf".
        self.threshold = max(floor, quantile([self.forest.score(r) for r in check], q) + margin)
        self.median, self.lo, self.hi = [], [], []
        for j in range(len(units)):
            col = sorted(r[j] for r in rows)
            self.median.append(statistics.median(col))
            self.lo.append(quantile(col, 0.01))
            self.hi.append(quantile(col, 0.99))
        self.samples = len(rows)
        self.pods = pods
        self.trained_at = trained_at
        self.preview = [[round(v, 6) for v in r] for r in rng.sample(rows, min(400, len(rows)))]

    def score(self, x: list[float]) -> float:
        return self.forest.score([v / u for v, u in zip(x, self.units)])

    def deviations(self, x: list[float]) -> list[float]:
        """"Neden?" sorusu için: her ölçüm tek başına eğitim verisinin %1–%99 aralığının ne kadar dışında
        (en küçük anlamlı değişim birimiyle; içerideyse 0, altındaysa eksi)."""
        return [(v - hi) / u if v > hi else (v - lo) / u if v < lo else 0.0
                for v, lo, hi, u in zip(x, self.lo, self.hi, self.units)]
