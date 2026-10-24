"""Tests for clipbot/editwords.py: word timing that a cut, a caption and a profanity check can rely on.

The faults being handled are the ones measured on real clips - words given no time, words stretched over a
pause - so each is built here from the real shape (a collapsed run, a three-letter word held for 1.4 s), and
the rule they exist for is pinned: a cut is never left inside a word it could have moved past.
"""

import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from clipbot import editwords as W
from clipbot.config import Settings
from clipbot.utils import StageError
from clipbot.workspace import Workspace


def w(text, s, e, **kw):
    return dict({"w": text, "s": s, "e": e}, **kw)


class TestRepair(unittest.TestCase):
    def test_a_collapsed_run_is_spread_over_the_gap_by_length(self):
        words = [w("pehla", 1.0, 1.4), w("baad", 8.36, 8.36), w("nahin", 8.36, 8.36), w("aaga", 8.36, 8.36), w("tha", 8.36, 8.74)]
        out = W.repair(words)
        run = out[1:4]
        self.assertTrue(all(x["est"] for x in run))
        self.assertTrue(all(x["e"] - x["s"] >= W.MIN_WORD for x in run))
        self.assertTrue(all(a["e"] <= b["s"] + 1e-6 for a, b in zip(out, out[1:])), out)

    def test_ordinary_words_are_untouched_and_not_marked(self):
        words = [w("a", 0.0, 0.3), w("b", 0.4, 0.8)]
        self.assertEqual(W.repair(words), words)

    def test_empty_words_are_dropped(self):
        self.assertEqual([x["w"] for x in W.repair([w("  ", 0, 1), w("ok", 1, 2)])], ["ok"])

    def test_a_collapsed_word_at_the_very_end_gets_some_time(self):
        out = W.repair([w("a", 0.0, 0.5), w("end", 0.9, 0.9)])
        self.assertGreater(out[1]["e"], out[1]["s"])


def envelope(spec):
    """A synthetic level trace: `spec` is [(seconds, level_db)], 10 ms per sample."""
    db = np.concatenate([np.full(int(d * 100), lvl) for d, lvl in spec])
    return np.arange(len(db)) / 100.0, db


class TestVoicedSpans(unittest.TestCase):
    def test_a_word_stretched_over_a_pause_is_trimmed_to_where_the_audio_is_up(self):
        times, db = envelope([(1.0, -50), (0.5, -20), (1.5, -50), (1.0, -20), (1.0, -50)])
        words = [w("haan", 1.0, 1.5), w("hain", 1.5, 3.0), w("bhai", 3.0, 4.0)]
        out = W.voiced_spans(words, times, db)
        self.assertTrue(out[1]["silent"])                          # "hain" is up for none of its 1.5 s (its last sample is bhai's onset)
        self.assertAlmostEqual(out[0]["ve"], 1.5, delta=0.1)
        self.assertAlmostEqual(out[2]["vs"], 3.0, delta=0.05)

    def test_a_word_where_there_is_no_sound_is_silent(self):
        times, db = envelope([(1.0, -20), (1.0, -55), (1.0, -20)])
        out = W.voiced_spans([w("ghost", 1.1, 1.8)], times, db)
        self.assertTrue(out[0]["silent"])

    def test_the_threshold_follows_the_window_not_a_fixed_level(self):
        loud_bed = envelope([(1.0, -30), (1.0, -10), (1.0, -30)])
        quiet_bed = envelope([(1.0, -60), (1.0, -40), (1.0, -60)])
        for times, db in (loud_bed, quiet_bed):
            out = W.voiced_spans([w("x", 1.0, 2.0)], times, db)
            self.assertFalse(out[0].get("silent"))
            self.assertAlmostEqual(out[0]["vs"], 1.0, delta=0.05)

    def test_the_runs_where_the_audio_is_up_are_kept_and_small_dips_are_bridged(self):
        times, db = envelope([(1.0, -50), (0.3, -20), (0.05, -50), (0.3, -20), (0.5, -50), (0.3, -20), (1.0, -50)])
        out = W.voiced_spans([w("lamba", 1.0, 2.5)], times, db)
        runs = out[0]["vi"]
        self.assertEqual(len(runs), 2)                               # the 50 ms dip is bridged, the 500 ms pause is not
        self.assertAlmostEqual(runs[0][0], 1.0, delta=0.03)
        self.assertAlmostEqual(runs[1][0], 2.15, delta=0.03)

    def test_too_little_audio_leaves_the_spans_as_given(self):
        out = W.voiced_spans([w("x", 1.0, 2.0)], np.zeros(3), np.zeros(3))
        self.assertEqual((out[0]["vs"], out[0]["ve"]), (1.0, 2.0))


def spoken(*items):
    return [dict(w(t, s, e), vs=s, ve=e) for t, s, e in items]


class TestInsideWord(unittest.TestCase):
    words = spoken(("main", 1.0, 1.4), ("nahi", 1.5, 1.9), ("chudi", 2.0, 2.6))

    def test_a_cut_in_the_middle_of_a_word_is_inside_it(self):
        self.assertEqual(W.inside_word(self.words, 1.7)["w"], "nahi")

    def test_a_cut_in_a_gap_is_not(self):
        self.assertIsNone(W.inside_word(self.words, 1.45))

    def test_a_cut_at_a_words_edge_is_not_inside_it(self):
        self.assertIsNone(W.inside_word(self.words, 1.5 + W.EDGE / 2))
        self.assertIsNone(W.inside_word(self.words, 1.9 - W.EDGE / 2))

    def test_a_silent_word_is_never_something_a_cut_splits(self):
        ws = [dict(w("ghost", 1.0, 2.0), vs=1.0, ve=1.0, silent=True)]
        self.assertIsNone(W.inside_word(ws, 1.5))

    def test_a_cut_in_a_long_dip_inside_a_stretched_word_is_not_inside_it(self):
        ws = [dict(w("lamba", 1.0, 2.5), vs=1.0, ve=2.5, vi=[[1.0, 1.3], [2.2, 2.5]])]
        self.assertIsNone(W.inside_word(ws, 1.75))
        self.assertIsNotNone(W.inside_word(ws, 1.15))
        self.assertIsNotNone(W.inside_word(ws, 2.35))

    def test_judged_on_the_voiced_span_not_the_stretched_one(self):
        ws = [dict(w("hain", 1.0, 3.0), vs=1.0, ve=1.3)]
        self.assertIsNone(W.inside_word(ws, 2.0))                   # in the pause the word was stretched over
        self.assertIsNotNone(W.inside_word(ws, 1.15))


class TestWordSnap(unittest.TestCase):
    words = spoken(("main", 1.0, 1.4), ("nahi", 1.5, 1.9), ("chudi", 2.0, 2.6))

    def test_an_in_cut_inside_a_word_moves_to_just_before_it(self):
        r = W.word_snap(self.words, 1.7, "in")
        self.assertTrue(r["snapped"])
        self.assertEqual(r["inside"], "nahi")
        self.assertLess(r["t"], 1.5)
        self.assertGreaterEqual(r["t"], 1.4 + 0.02 - 1e-9)           # not back into the word before
        self.assertIsNone(W.inside_word(self.words, r["t"]))

    def test_an_out_cut_inside_a_word_moves_to_just_after_it(self):
        r = W.word_snap(self.words, 2.2, "out")
        self.assertTrue(r["snapped"])
        self.assertGreater(r["t"], 2.6)
        self.assertIsNone(W.inside_word(self.words, r["t"]))

    def test_a_cut_in_a_gap_is_left_alone(self):
        r = W.word_snap(self.words, 1.45, "in")
        self.assertEqual((r["t"], r["snapped"], r["inside"]), (1.45, False, None))

    def test_a_move_longer_than_the_limit_is_not_made_and_the_word_is_named(self):
        ws = spoken(("laaambaaa", 1.0, 4.0))
        r = W.word_snap(ws, 2.5, "out", max_move=0.6)
        self.assertFalse(r["snapped"])
        self.assertEqual(r["inside"], "laaambaaa")
        self.assertEqual(r["t"], 2.5)

    def test_words_run_together_move_past_all_of_them_or_not_at_all(self):
        ws = spoken(("a", 1.0, 1.3), ("b", 1.3, 1.6), ("c", 1.6, 1.9))
        r = W.word_snap(ws, 1.45, "out")
        self.assertTrue(r["snapped"])
        self.assertIsNone(W.inside_word(ws, r["t"]))

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            W.word_snap(self.words, 1.7, "middle")


class TestStrongWords(unittest.TestCase):
    def test_latin_and_devanagari_and_masked_forms(self):
        self.assertTrue(W.strong_words("what the fuck"))
        self.assertTrue(W.strong_words("F**k"))
        self.assertTrue(W.strong_words("बेहन की चूथ"))
        self.assertTrue(W.strong_words("bhai madarchod hai"))

    def test_a_short_word_is_matched_whole_not_inside_another_word(self):
        self.assertEqual(W.strong_words("parachute hai na parachute"), [])
        self.assertEqual(W.strong_words("mcdonalds and abcd"), [])
        self.assertTrue(W.strong_words("arre chut"))
        self.assertTrue(W.strong_words("what a bc"))

    def test_a_stem_matches_its_endings(self):
        self.assertTrue(W.strong_words("fucking hell"))
        self.assertTrue(W.strong_words("chutiyapa hai"))

    def test_clean_speech_is_clean(self):
        self.assertEqual(W.strong_words("hum pe mortar chalu ho gaya guys"), [])
        self.assertEqual(W.strong_words("मैं नहीं चुदी"), ["चुद"])        # the list errs towards flagging: it is a warning


def make_ws(tmp):
    ws = Workspace(Path(tmp) / "w")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.write_json(ws.root / "state.json", {"duration": 100.0})
    rate = 16000
    t = np.arange(rate * 20) / rate
    x = (0.4 * np.sin(2 * np.pi * 200 * t) * ((t % 2.0) < 1.0)).astype("<i2") if False else (3000 * np.sin(2 * np.pi * 200 * t) * ((t % 2.0) < 1.0)).astype("<i2")
    with wave.open(str(ws.audio_path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(x.tobytes())
    return ws


def ffmpeg_path():
    try:
        from clipbot.config import load_settings
        from clipbot.utils import resolve_tool
        return resolve_tool(load_settings().tool("ffmpeg"), "")
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


class FakeClient:
    def __init__(self, words, calls):
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))
        self.words, self.calls = words, calls

    def create(self, file=None, **kw):
        self.calls.append(kw)
        return SimpleNamespace(words=[SimpleNamespace(word=t, start=s, end=e) for t, s, e in self.words], text=" ".join(t for t, _, _ in self.words))


@unittest.skipUnless(ffmpeg_path(), "ffmpeg is not available")
class TestFetchAndStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self._tmp.name)
        self.settings = Settings({"work_root": self._tmp.name, "tools": {"ffmpeg": ffmpeg_path()}, "transcribe": {"language": "hi"}},
                                 Path("settings.json"))              # the stream's transcript is pinned to Hindi; the words must not be

    def with_words(self, **words):
        return Settings({"work_root": self._tmp.name, "tools": {"ffmpeg": ffmpeg_path()}, "words": words}, Path("settings.json"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_fetch_returns_words_in_the_streams_own_seconds_repaired_with_voiced_spans(self):
        calls = []
        fake = FakeClient([("ek", 0.2, 0.7), ("dum", 0.7, 0.7), ("bhai", 2.2, 2.8)], calls)
        win = W.fetch_window(self.settings, self.ws, 10.0, 16.0, client=fake)
        self.assertEqual(win["a"], 10.0)
        self.assertAlmostEqual(win["words"][0]["s"], 10.2, places=2)           # window-relative -> stream clock
        self.assertTrue(win["words"][1].get("est"))                              # the collapsed word was repaired
        self.assertIn("vs", win["words"][0])
        self.assertEqual(calls[0]["timestamp_granularities"], ["word"])
        self.assertEqual(calls[0]["model"], "whisper-1")

    def test_the_language_is_not_pinned_to_the_streams_transcript_language_and_a_hinglish_prompt_is_sent(self):
        calls = []
        W.fetch_window(self.settings, self.ws, 10.0, 16.0, client=FakeClient([("ek", 0.2, 0.7)], calls))
        self.assertNotIn("language", calls[0])                          # transcribe.language is "hi" here; words ignore it
        self.assertEqual(calls[0]["prompt"], W.HINGLISH_PROMPT)

    def test_a_language_and_an_empty_prompt_can_be_set(self):
        calls = []
        W.fetch_window(self.with_words(language="en", prompt=""), self.ws, 10.0, 16.0, client=FakeClient([("ek", 0.2, 0.7)], calls))
        self.assertEqual(calls[0]["language"], "en")
        self.assertNotIn("prompt", calls[0])

    def test_auto_spelled_out_is_no_language(self):
        calls = []
        W.fetch_window(self.with_words(language="auto"), self.ws, 10.0, 16.0, client=FakeClient([("ek", 0.2, 0.7)], calls))
        self.assertNotIn("language", calls[0])

    def test_a_window_fetched_under_other_settings_is_not_reused_and_is_replaced(self):
        calls = []
        fake = FakeClient([("ek", 3.0, 3.5)], calls)
        W.words_for(self.settings, self.ws, 10.0, 14.0, client=fake)
        self.assertIsNone(W.words_for(self.with_words(language="hi"), self.ws, 10.0, 14.0, fetch=False, client=fake))
        W.words_for(self.with_words(language="hi"), self.ws, 10.0, 14.0, client=fake)
        self.assertEqual(len(calls), 2)
        doc = W._load_store(W.store_path(self.settings, self.ws))
        self.assertEqual(len(doc["windows"]), 1)                        # the stale one went when the new one came
        self.assertEqual(doc["windows"][0]["sig"], W.signature(self.with_words(language="hi")))

    def test_symbols_and_emoji_the_recogniser_writes_for_noise_are_not_words(self):
        win = W.fetch_window(self.settings, self.ws, 10.0, 16.0, client=FakeClient([("🔥🔥🔥", 0.2, 0.9), ("bhai", 1.0, 1.4), ("...", 1.5, 1.6)], []))
        self.assertEqual([w["w"] for w in win["words"]], ["bhai"])

    def test_a_window_the_recogniser_answered_with_the_prompt_has_no_words(self):
        echo = [(w, 0.2 + 0.3 * i, 0.4 + 0.3 * i) for i, w in enumerate(W.HINGLISH_PROMPT.split())]
        win = W.fetch_window(self.settings, self.ws, 10.0, 16.0, client=FakeClient(echo, []))
        self.assertEqual(win["words"], [])

    def test_a_second_request_inside_a_stored_window_makes_no_call(self):
        calls = []
        fake = FakeClient([("ek", 3.0, 3.5)], calls)
        a = W.words_for(self.settings, self.ws, 10.0, 14.0, client=fake)
        b = W.words_for(self.settings, self.ws, 10.5, 13.5, client=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)

    def test_fetch_false_returns_none_when_nothing_is_stored_and_makes_no_call(self):
        calls = []
        self.assertIsNone(W.words_for(self.settings, self.ws, 10.0, 14.0, fetch=False, client=FakeClient([], calls)))
        self.assertEqual(calls, [])

    def test_a_window_the_store_does_not_cover_is_fetched(self):
        calls = []
        fake = FakeClient([("ek", 3.0, 3.5)], calls)
        W.words_for(self.settings, self.ws, 10.0, 14.0, client=fake)
        W.words_for(self.settings, self.ws, 60.0, 64.0, client=fake)
        self.assertEqual(len(calls), 2)

    def test_a_failing_api_is_a_stage_error_naming_the_window(self):
        class Boom:
            audio = SimpleNamespace(transcriptions=SimpleNamespace(create=mock.Mock(side_effect=RuntimeError("rate limited"))))

        with mock.patch.object(W.time, "sleep"), self.assertRaises(StageError) as ctx:
            W.fetch_window(self.settings, self.ws, 10.0, 14.0, client=Boom())
        self.assertIn("rate limited", str(ctx.exception))

    def test_no_audio_is_an_error_that_says_what_to_run(self):
        self.ws.audio_path.unlink()
        with self.assertRaises(StageError):
            W.fetch_window(self.settings, self.ws, 0.0, 4.0, client=FakeClient([], []))

    def test_the_estimate_is_the_published_price(self):
        self.assertAlmostEqual(W.estimate_cost(600.0), 0.06)


class TestEchoesPrompt(unittest.TestCase):
    def test_the_prompt_coming_back_is_an_echo(self):
        self.assertTrue(W.echoes_prompt(W.HINGLISH_PROMPT, W.HINGLISH_PROMPT))
        self.assertTrue(W.echoes_prompt("Yaar bhai, wait wait, ye kya ho gaya?", W.HINGLISH_PROMPT))

    def test_speech_that_happens_to_share_words_with_the_prompt_is_not(self):
        self.assertFalse(W.echoes_prompt("wait bro wait", W.HINGLISH_PROMPT))
        self.assertFalse(W.echoes_prompt("oh my god", W.HINGLISH_PROMPT))
        self.assertFalse(W.echoes_prompt("ye kya ho gaya bhai mortar gir gaya bro help", W.HINGLISH_PROMPT))

    def test_the_prompt_followed_by_a_lot_of_real_speech_is_kept(self):
        self.assertFalse(W.echoes_prompt(W.HINGLISH_PROMPT + " " + "aur phir mortar gir gaya mere upar bhai " * 3, W.HINGLISH_PROMPT))

    def test_nothing_is_not_an_echo(self):
        self.assertFalse(W.echoes_prompt("", W.HINGLISH_PROMPT))
        self.assertFalse(W.echoes_prompt("hello", ""))


if __name__ == "__main__":
    unittest.main()
