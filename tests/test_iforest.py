import math
import random
import unittest

from nobetci.iforest import IsolationForest, Model, c

MiB = 1024 * 1024
UNITS = [0.1, 64 * MiB, 100e3, 100e3]


def busy_rows(n=2000, seed=1):
    """CPU ile gelen trafiğin gün içinde birlikte artıp azaldığı bir iş yükü."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        load = 1 + 0.4 * math.sin(2 * math.pi * i / 288)
        rows.append([0.6 * load * (1 + 0.03 * rng.uniform(-1, 1)), 120 * MiB * (1 + 0.01 * rng.uniform(-1, 1)),
                     2e6 * load * (1 + 0.03 * rng.uniform(-1, 1)), 50e3])
    return rows


class ForestTest(unittest.TestCase):
    def test_c(self):
        self.assertEqual(c(1), 0)
        self.assertEqual(c(2), 1)
        self.assertAlmostEqual(c(256), 10.24, places=1)

    def test_outlier_isolates_faster(self):
        rng = random.Random(0)
        pts = [[rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(500)]
        f = IsolationForest(trees=60, seed=2).fit(pts, jitter=0)
        self.assertGreater(f.score([8, 8]), f.score([0, 0]) + 0.15)

    def test_far_outside_range_scores_higher_than_edge(self):
        rng = random.Random(0)
        pts = [[rng.uniform(0, 1)] for _ in range(500)]
        f = IsolationForest(trees=60, seed=2).fit(pts, jitter=0)
        self.assertGreater(f.score([5.0]), f.score([1.01]) + 0.1)

    def test_deterministic(self):
        rows = busy_rows(400)
        a = Model(rows, UNITS, trees=30, sample=128, seed=7, q=0.995, trained_at=0, pods=1)
        b = Model(rows, UNITS, trees=30, sample=128, seed=7, q=0.995, trained_at=0, pods=1)
        self.assertEqual(a.score(rows[3]), b.score(rows[3]))


class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Model(busy_rows(), UNITS, trees=100, sample=256, seed=3, q=0.995, trained_at=0, pods=1)

    def flagged(self, cpu, mem_mib, rx, tx=50e3):
        return self.m.score([cpu, mem_mib * MiB, rx, tx]) >= self.m.threshold

    def test_normal_points_are_quiet(self):
        for cpu, rx in ((0.6, 2e6), (0.84, 2.8e6), (0.36, 1.2e6)):  # ortalama, gün tepesi, gece
            self.assertFalse(self.flagged(cpu, 120, rx), (cpu, rx))

    def test_training_data_mostly_below_threshold(self):
        rows = busy_rows(1000, seed=9)
        above = sum(self.m.score(r) >= self.m.threshold for r in rows)
        self.assertLessEqual(above, 5)

    def test_combination_only_ml_sees(self):
        # CPU gündüz seviyesinde, trafik gece seviyesinde: ikisi de tek başına olağan aralıkta.
        self.assertTrue(self.flagged(0.75, 120, 1.25e6))
        self.assertEqual(self.m.deviations([0.75, 120 * MiB, 1.25e6, 50e3]), [0, 0, 0, 0])

    def test_single_metric_jump(self):
        self.assertTrue(self.flagged(1.5, 120, 2e6))
        self.assertGreater(self.m.deviations([1.5, 120 * MiB, 2e6, 50e3])[0], 5)

    def test_tiny_change_on_flat_metric_is_ignored(self):
        # gidenler sabit 50 KB/s; 30 KB/s'lik oynama en küçük anlamlı değişimin (100 KB/s) altında
        self.assertFalse(self.flagged(0.6, 120, 2e6, 80e3))
        self.assertFalse(self.flagged(0.6, 125, 2e6))


def hour_of(i):
    return (i * 5 / 60) % 24  # busy_rows 5 dakikalık adımlarla; yük 06:00'da tepe, 18:00'de dip


class HourTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = busy_rows()
        cls.m = Model(rows, UNITS, trees=100, sample=256, seed=3, q=0.995, trained_at=0, pods=1,
                      hours=[hour_of(i) for i in range(len(rows))])

    def test_peak_load_is_normal_at_peak_hour(self):
        x = [0.84, 120 * MiB, 2.8e6, 50e3]
        self.assertLess(self.m.score(x, 6.0), self.m.threshold)

    def test_peak_load_at_quiet_hour_is_flagged(self):
        # Gündüz tepesindeki yük gecenin dibinde: değerler tek tek olağan, saatle birlikte değil.
        x = [0.84, 120 * MiB, 2.8e6, 50e3]
        self.assertGreaterEqual(self.m.score(x, 18.0), self.m.threshold)
        self.assertEqual(self.m.deviations(x), [0, 0, 0, 0])
        self.assertGreater(self.m.deviations(x, 18.0)[0], 0)

    def test_midnight_wraps(self):
        self.assertAlmostEqual(self.m.score([0.6, 120 * MiB, 2e6, 50e3], 23.99),
                               self.m.score([0.6, 120 * MiB, 2e6, 50e3], 0.0), delta=0.03)


class FeedbackTest(unittest.TestCase):
    def test_false_alarm_raises_threshold(self):
        rows = busy_rows()
        odd = [0.75, 120 * MiB, 1.25e6, 50e3]
        plain = Model(rows, UNITS, trees=100, sample=256, seed=3, q=0.995, trained_at=0, pods=1)
        self.assertGreaterEqual(plain.score(odd), plain.threshold)
        taught = Model(rows + [odd] * 60, UNITS, trees=100, sample=256, seed=3, q=0.995, trained_at=0, pods=1,
                       accept=[(odd, None)] * 3)
        self.assertLess(taught.score(odd), taught.threshold)
        self.assertLessEqual(taught.threshold, taught.base_threshold + 0.15)
        self.assertEqual(taught.accepted, 3)


if __name__ == "__main__":
    unittest.main()
