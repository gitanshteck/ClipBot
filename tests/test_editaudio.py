"""Tests for clipbot/editaudio.py: peak alignment, beat grid, join-click score."""

import unittest

import numpy as np

from clipbot import editaudio as A

SR = A.ANALYSIS_RATE


def click_at(t, seconds=1.0, amp=1.0):
    x = np.zeros(int(SR * seconds), dtype=np.float32)
    x[int(t * SR)] = amp
    return x


class TestPeak(unittest.TestCase):
    def test_finds_the_loudest_instant(self):
        info = A.find_peak(click_at(0.37), SR)
        self.assertAlmostEqual(info["peak_at"], 0.37, delta=0.01)
        self.assertAlmostEqual(info["duration"], 1.0)
        self.assertAlmostEqual(info["peak_db"], 0.0, places=3)

    def test_a_loud_hit_beats_a_quiet_tail(self):
        x = click_at(0.8, amp=0.2)
        x[int(0.1 * SR):int(0.1 * SR) + 200] = 0.9
        self.assertAlmostEqual(A.find_peak(x, SR)["peak_at"], 0.1, delta=0.02)

    def test_silence_and_empty(self):
        self.assertEqual(A.find_peak(np.zeros(0, dtype=np.float32), SR)["duration"], 0.0)
        self.assertLess(A.find_peak(np.zeros(SR, dtype=np.float32), SR)["peak_db"], -100)


class TestSfxStart(unittest.TestCase):
    def test_peak_lands_on_the_hit(self):
        start, trimmed = A.sfx_start(10.0, 0.2, 0.0, "peak")
        self.assertAlmostEqual(start + 0.2, 10.0)
        self.assertEqual(trimmed, 0.0)

    def test_lead_puts_the_sound_slightly_early(self):
        start, _ = A.sfx_start(10.0, 0.2, 0.05, "peak")
        self.assertAlmostEqual(start + 0.2, 9.95)

    def test_start_alignment_ignores_the_peak(self):
        self.assertEqual(A.sfx_start(10.0, 0.2, 0.0, "start"), (10.0, 0.0))

    def test_before_the_video_start_it_clamps_and_reports(self):
        start, trimmed = A.sfx_start(0.1, 0.3, 0.0, "peak")
        self.assertEqual(start, 0.0)
        self.assertAlmostEqual(trimmed, 0.2)


class TestBeats(unittest.TestCase):
    def _track(self, bpm=120.0, seconds=12.0, phase=0.25):
        n = int(SR * seconds)
        x = np.random.RandomState(1).randn(n).astype(np.float32) * 0.01
        period = 60.0 / bpm
        t = phase
        while t < seconds - 0.1:
            i = int(t * SR)
            burst = np.hanning(400).astype(np.float32) * np.sin(np.arange(400) * 0.4).astype(np.float32)
            x[i:i + 400] += burst * 0.8
            t += period
        return x

    def test_tempo_and_phase_of_a_steady_pulse(self):
        res = A.detect_beats(self._track(120.0))
        self.assertIsNotNone(res["bpm"])
        self.assertAlmostEqual(res["bpm"], 120.0, delta=4.0)
        self.assertAlmostEqual(res["beats"][0] % 0.5, 0.25, delta=0.06)

    def test_no_pulse_no_beats(self):
        res = A.detect_beats(np.random.RandomState(2).randn(SR * 6).astype(np.float32) * 0.01)
        self.assertEqual(res["beats"] == [], res["bpm"] is None)

    def test_too_short_to_analyse(self):
        self.assertEqual(A.detect_beats(np.zeros(100, dtype=np.float32)), {"bpm": None, "beats": []})

    def test_snap_within_tolerance_only(self):
        beats = [0.5, 1.0, 1.5]
        self.assertEqual(A.snap_to_beat(1.05, beats), 1.0)
        self.assertEqual(A.snap_to_beat(1.3, beats), 1.3)
        self.assertEqual(A.snap_to_beat(1.3, []), 1.3)


class TestClickScore(unittest.TestCase):
    def test_a_hard_step_scores_high_and_a_sine_low(self):
        t = np.arange(SR * 2) / float(SR)
        smooth = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        cut = smooth.copy()
        cut[SR:] = (0.3 * np.sin(2 * np.pi * 220 * t[SR:] + 2.0)).astype(np.float32)   # phase jump at 1.0 s
        self.assertLess(A.click_score(smooth, SR, 1.0), 6.0)
        self.assertGreater(A.click_score(cut, SR, 1.0), A.click_score(smooth, SR, 1.0) * 2)

    def test_edges_do_not_crash(self):
        x = np.zeros(100, dtype=np.float32)
        self.assertEqual(A.click_score(x, SR, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
