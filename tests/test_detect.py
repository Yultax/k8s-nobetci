import unittest

from nobetci.detect import HourProfile, Jump, detect_fill, detect_jump
from nobetci.llm import parse
from nobetci.signals import workload_of

FLAT = [0.10, 0.11, 0.09, 0.10, 0.12, 0.10, 0.09, 0.11] * 5


class JumpTest(unittest.TestCase):
    def test_flat_series_is_quiet(self):
        self.assertIsNone(detect_jump(FLAT, min_abs=0.1))

    def test_spike_needs_persistence(self):
        self.assertIsNone(detect_jump(FLAT + [0.9], min_abs=0.1))
        jump = detect_jump(FLAT + [0.9, 0.95], min_abs=0.1)
        self.assertEqual(jump.direction, "up")
        self.assertAlmostEqual(jump.baseline, 0.10, places=2)

    def test_small_change_below_min_abs_is_ignored(self):
        self.assertIsNone(detect_jump(FLAT + [0.17, 0.17], min_abs=0.1))

    def test_drop_needs_halving(self):
        busy = [v * 10 for v in FLAT]
        self.assertIsNone(detect_jump(busy + [0.7, 0.7], min_abs=0.1))
        self.assertEqual(detect_jump(busy + [0.05, 0.04], min_abs=0.1).direction, "down")

    def test_warmup(self):
        self.assertIsNone(detect_jump([0.1] * 10 + [0.9, 0.9], min_abs=0.1))

    def test_noisy_series_tolerates_its_own_noise(self):
        noisy = [1.0, 2.0, 0.5, 1.8, 0.7, 2.2, 1.1, 0.6] * 5
        self.assertIsNone(detect_jump(noisy + [2.4, 2.3], min_abs=0.1))


class ProfileTest(unittest.TestCase):
    def profile(self, days):
        p = HourProfile()
        for d in range(days):
            for minute in range(0, 1440, 5):
                # her gün 03:00-03:59 arası yedekleme işi
                p.add(d * 86400 + minute * 60, 0.8 if 180 <= minute < 240 else 0.1)
        return p

    def test_daily_job_is_expected(self):
        p = self.profile(5)
        at_3 = 5 * 86400 + 3 * 3600 + 600
        self.assertTrue(p.expected(Jump("up", 0.85, 0.1, 9), at_3))
        self.assertFalse(p.expected(Jump("up", 0.85, 0.1, 9), at_3 + 6 * 3600))
        self.assertFalse(p.expected(Jump("up", 2.0, 0.1, 9), at_3))

    def test_needs_enough_days(self):
        self.assertIsNone(self.profile(2).band(3 * 3600))

    def test_single_bad_day_does_not_poison(self):
        p = self.profile(5)
        p.add(2 * 86400 + 12 * 3600, 5.0)  # bir gün öğlen yaşanmış anomali
        self.assertFalse(p.expected(Jump("up", 4.0, 0.1, 9), 5 * 86400 + 12 * 3600 + 60))


class FillTest(unittest.TestCase):
    def test_leak_gives_eta(self):
        ts = [i * 60.0 for i in range(60)]
        vs = [200.0] * 35 + [200.0 + 4 * i for i in range(25)]  # son 25 dk'da dakikada 4 birim
        eta = detect_fill(ts, vs, 512.0, horizon=6 * 3600)
        self.assertAlmostEqual(eta / 60, (512 - vs[-1]) / 4, delta=3)

    def test_flat_and_far_are_quiet(self):
        ts = [i * 60.0 for i in range(60)]
        self.assertIsNone(detect_fill(ts, [200.0 + (i % 3) for i in range(60)], 512.0, horizon=6 * 3600))
        slow = [200.0 + 0.5 * i for i in range(60)]
        self.assertIsNone(detect_fill(ts, slow, 512.0, horizon=3600))


class HelpersTest(unittest.TestCase):
    def test_workload_of(self):
        self.assertEqual(workload_of("prometheus-server-f4f76b6b-qjwf5"), "prometheus-server")
        self.assertEqual(workload_of("kube-proxy-x7k2p"), "kube-proxy")
        self.assertEqual(workload_of("cache-0"), "cache")
        self.assertEqual(workload_of("etcd-service-monitoring-control-plane"), "etcd-service-monitoring-control-plane")

    def test_llm_parse(self):
        text = "**ÖZET:** Pod çöktü.\nNEDEN: Bellek\nyetmedi.\n- ÖNERİ: Limiti artırın."
        self.assertEqual(parse(text), {"ozet": "Pod çöktü.", "neden": "Bellek yetmedi.", "oneri": "Limiti artırın."})
        self.assertIsNone(parse("Merhaba, bu bir olay."))


if __name__ == "__main__":
    unittest.main()
