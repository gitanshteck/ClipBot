"""Tests for clipbot/editcut.py: pause-aware cut points and level matching, on synthetic audio."""

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from clipbot import editcut as C

RATE = 16000


def tone(seconds, amp=0.3, freq=220.0):
    t = np.arange(int(seconds * RATE)) / float(RATE)
    return amp * np.sin(2 * np.pi * freq * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 5 * t))   # syllable-ish wobble


def quiet(seconds, amp=0.001):
    return np.random.RandomState(1).randn(int(seconds * RATE)) * amp


def write_wav(path, samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def talk(*parts):
    """('s', seconds) = speech, ('p', seconds) = a pause."""
    return np.concatenate([tone(d) if k == "s" else quiet(d) for k, d in parts])


class TestEnvelope(unittest.TestCase):
    def test_level_follows_the_signal(self):
        t, db = C.envelope(talk(("s", 1.0), ("p", 1.0)), RATE)
        self.assertGreater(float(np.mean(db[(t > 0.2) & (t < 0.8)])), float(np.mean(db[(t > 1.2) & (t < 1.8)])) + 30)

    def test_too_short_a_signal_has_no_envelope(self):
        t, db = C.envelope(np.zeros(100), RATE)
        self.assertEqual(len(t), 0)


class TestPauses(unittest.TestCase):
    def test_finds_the_gaps_between_phrases(self):
        t, db = C.envelope(talk(("s", 2.0), ("p", 0.6), ("s", 2.0), ("p", 1.0), ("s", 1.5)), RATE)
        gaps = C.find_pauses(t, db)
        self.assertEqual(len(gaps), 2)
        self.assertAlmostEqual(gaps[0]["start"], 2.0, delta=0.15)
        self.assertAlmostEqual(gaps[0]["end"], 2.6, delta=0.15)
        self.assertGreater(gaps[0]["depth"], 30)

    def test_a_gap_shorter_than_min_len_is_ignored(self):
        t, db = C.envelope(talk(("s", 2.0), ("p", 0.1), ("s", 2.0)), RATE)
        self.assertEqual(C.find_pauses(t, db, min_len=0.2), [])

    def test_steady_noise_has_no_pauses(self):
        t, db = C.envelope(tone(6.0, amp=0.2, freq=300.0) * 0 + np.random.RandomState(2).randn(6 * RATE) * 0.05, RATE)
        self.assertEqual(C.find_pauses(t, db), [])

    def test_mostly_quiet_talk_still_has_its_gaps(self):
        # a median-based threshold would sit below the noise floor here; the range-based one does not
        t, db = C.envelope(talk(("p", 2.5), ("s", 1.0), ("p", 2.5), ("s", 1.0), ("p", 2.0)), RATE)
        self.assertGreaterEqual(len(C.find_pauses(t, db)), 3)

    def test_cut_points_sit_inside_the_gap(self):
        gap = {"start": 10.0, "end": 12.0, "depth": 30.0}
        self.assertAlmostEqual(C.cut_point(gap, "in"), 12.0 - C.IN_ROOM)
        self.assertAlmostEqual(C.cut_point(gap, "out"), 10.0 + C.OUT_TAIL)
        short = {"start": 10.0, "end": 10.3, "depth": 30.0}
        self.assertAlmostEqual(C.cut_point(short, "in"), 10.15)
        self.assertAlmostEqual(C.cut_point(short, "out"), 10.15)


class TestFindCut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wav = Path(self.tmp.name) / "audio.wav"
        # speech 0-6, pause 6-7.5, speech 7.5-12, pause 12-12.8, speech 12.8-20
        write_wav(self.wav, np.concatenate([talk(("s", 6.0), ("p", 1.5), ("s", 4.5), ("p", 0.8), ("s", 7.2))]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_out_point_just_inside_speech_moves_into_the_gap(self):
        r = C.find_cut(self.wav, 5.7, "out")
        self.assertTrue(r["snapped"])
        self.assertAlmostEqual(r["t"], 6.0 + C.OUT_TAIL, delta=0.12)
        self.assertLess(r["moved"], 0.8)

    def test_an_in_point_lands_just_before_the_next_phrase(self):
        r = C.find_cut(self.wav, 7.9, "in")
        self.assertTrue(r["snapped"])
        self.assertAlmostEqual(r["t"], 7.5 - C.IN_ROOM, delta=0.12)

    def test_a_cut_far_from_any_gap_is_left_alone(self):
        r = C.find_cut(self.wav, 9.5, "out", window=0.8)
        self.assertFalse(r["snapped"])
        self.assertEqual(r["t"], 9.5)
        self.assertEqual(r["moved"], 0.0)

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            C.find_cut(self.wav, 5.0, "middle")

    def test_unreadable_wav_is_a_stage_error(self):
        from clipbot.utils import StageError
        with self.assertRaises(StageError):
            C.find_cut(Path(self.tmp.name) / "missing.wav", 5.0, "out")


class TestLevelsAndLineup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = Path(self.tmp.name) / "a.wav"
        self.b = Path(self.tmp.name) / "b.wav"
        # a: speech at full level, b: the same speech 12 dB quieter
        write_wav(self.a, talk(("s", 5.0), ("p", 1.0), ("s", 5.0), ("p", 1.0), ("s", 5.0)))
        write_wav(self.b, talk(("s", 5.0), ("p", 1.0), ("s", 5.0), ("p", 1.0), ("s", 5.0)) * 0.25)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_quieter_clip_measures_quieter(self):
        la, lb = C.clip_level(self.a, 0.5, 10.0), C.clip_level(self.b, 0.5, 10.0)
        self.assertAlmostEqual(la - lb, 12.0, delta=1.0)

    def lineup(self):
        return {"cold_open": {"clips": [{"src": "a", "in": 5.7, "out": 9.0}]},
                "chapters": [{"title": "x", "clips": [
                    {"src": "b", "in": 5.5, "out": 11.0},
                    {"src": "a", "in": 6.1, "out": 15.0, "kills": [{"t": 7.0}]},
                    {"src": "a", "in": 2.0, "out": 4.0, "snap": False},
                    {"src": "gone", "in": 1.0, "out": 3.0},
                    {"src": "a", "in": 1.0, "out": 4.0, "gain_db": -3.0}]}]}

    def wav_for(self, alias):
        return {"a": self.a, "b": self.b}.get(alias)

    def test_prepare_snaps_matches_and_respects_exceptions(self):
        lu = self.lineup()
        report = C.prepare_lineup(lu, self.wav_for)
        cold = lu["cold_open"]["clips"][0]
        self.assertNotEqual(cold["in"], 5.7)                       # snapped into the 5-6 s gap
        self.assertAlmostEqual(cold["in"], 6.0 - C.IN_ROOM + 0.0, delta=0.9)
        chapter = lu["chapters"][0]["clips"]
        self.assertEqual((chapter[1]["in"], chapter[1]["out"]), (6.1, 15.0))      # kills: window kept
        self.assertEqual((chapter[2]["in"], chapter[2]["out"]), (2.0, 4.0))       # snap: false
        self.assertEqual((chapter[3]["in"], chapter[3]["out"]), (1.0, 3.0))       # no audio: as authored
        self.assertNotIn("gain_db", chapter[3])
        self.assertEqual(chapter[4]["gain_db"], -3.0)                             # author's gain kept
        self.assertGreater(chapter[0]["gain_db"], 0.0)                            # the quiet clip is lifted
        self.assertEqual(len(report["clips"]), 6)
        self.assertGreaterEqual(report["moved"], 1)

    def test_gain_is_bounded(self):
        write_wav(self.b, talk(("s", 5.0), ("p", 1.0), ("s", 5.0), ("p", 1.0), ("s", 5.0)) * 0.02)
        lu = {"chapters": [{"title": "x", "clips": [{"src": "b", "in": 1.0, "out": 9.0}, {"src": "a", "in": 1.0, "out": 9.0}]}]}
        C.prepare_lineup(lu, self.wav_for, snap=False)
        self.assertLessEqual(lu["chapters"][0]["clips"][0]["gain_db"], C.MATCH_LIMIT_DB)

    def test_snap_and_match_can_be_turned_off(self):
        lu = self.lineup()
        C.prepare_lineup(lu, self.wav_for, snap=False, match=False)
        self.assertEqual(lu["cold_open"]["clips"][0]["in"], 5.7)
        self.assertNotIn("gain_db", lu["chapters"][0]["clips"][0])


if __name__ == "__main__":
    unittest.main()
