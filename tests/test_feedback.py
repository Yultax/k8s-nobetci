import math
import random
import unittest

from nobetci.config import Config
from nobetci.engine import Engine

MiB = 1024 * 1024
T0 = 1_700_000_000 - 1_700_000_000 % 86400  # UTC gece yarısı


def week(odd_from=None, odd_to=None):
    """5 dakikalık adımlarla 3 günlük veri; isteğe bağlı aralıkta CPU yüksek, trafik düşük (tuhaf birleşim)."""
    rng = random.Random(1)
    at = {}
    for i in range(3 * 288):
        t = T0 + i * 300
        load = 1 + 0.4 * math.sin(2 * math.pi * i / 288)
        row = [0.6 * load * (1 + 0.03 * rng.uniform(-1, 1)), 120 * MiB, 2e6 * load * (1 + 0.03 * rng.uniform(-1, 1)), 50e3]
        if odd_from is not None and odd_from <= t <= odd_to:
            row = [0.75, 120 * MiB, 1.25e6, 50e3]
        at[t] = row
    return {("demo", "isci-7d9f8b6c5d-x2k4p"): at}


def engine(**kw):
    return Engine(Config(ml_trees=60, tz_offset=0, **kw), prom=None)


def record(verdict, start, end, kinds=("ml",), signals=()):
    return {"incident": 1, "verdict": verdict, "scope": "pod", "ns": "demo", "wl": "isci",
            "start": start, "end": end, "kinds": list(kinds), "signals": list(signals)}


class FeedbackTest(unittest.TestCase):
    odd = (T0 + 2 * 86400 + 6 * 3600, T0 + 2 * 86400 + 7 * 3600)  # 3. gün 06:00–07:00

    def score(self, e):
        m = e.models[("demo", "isci")]
        return m.score([0.75, 120 * MiB, 1.25e6, 50e3], 6.5), m

    def test_without_feedback_model_trains_itself(self):
        e = engine()
        e.train(week(*self.odd), T0 + 3 * 86400)
        self.assertIn(("demo", "isci"), e.models)
        self.assertTrue(e.models[("demo", "isci")].uses_hour)

    def test_false_alarm_teaches_model(self):
        plain = engine()  # alarm anındaki model bu birleşimi henüz görmemişti
        plain.train(week(), T0 + 3 * 86400)
        before, m0 = self.score(plain)
        self.assertGreaterEqual(before, m0.threshold)
        e = engine()
        e.set_feedback([record("yanlis", *self.odd)])
        self.assertTrue(e.retrain_wanted)
        e.train(week(*self.odd), T0 + 3 * 86400)
        after, m = self.score(e)
        self.assertLess(after, m.threshold)
        self.assertGreater(m.feedback["accepted"], 0)

    def test_true_alarm_is_left_out_of_training(self):
        e = engine()
        e.set_feedback([record("dogru", *self.odd)])
        e.train(week(*self.odd), T0 + 3 * 86400)
        plain = engine()
        plain.train(week(*self.odd), T0 + 3 * 86400)
        self.assertLess(e.models[("demo", "isci")].samples, plain.models[("demo", "isci")].samples)

    def test_false_spike_raises_threshold_for_that_signal_only(self):
        e = engine()
        e.set_feedback([record("yanlis", 0, 1, kinds=("spike",), signals=("pod_cpu",))])
        self.assertEqual(e.z_boost, {("pod_cpu", "demo", "isci"): 1.25})
        self.assertFalse(e.retrain_wanted)
        e.set_feedback([record("yanlis", 0, 1, kinds=("spike",), signals=("pod_cpu",))] * 5)
        self.assertEqual(e.z_boost[("pod_cpu", "demo", "isci")], 2.0)

    def test_window_growth_does_not_retrain(self):
        e = engine()
        e.set_feedback([record("yanlis", 0, 100)])
        e.retrain_wanted = False
        e.set_feedback([record("yanlis", 0, 200)])
        self.assertFalse(e.retrain_wanted)


if __name__ == "__main__":
    unittest.main()
