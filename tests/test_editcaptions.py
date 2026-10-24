"""Tests for clipbot/editcaptions.py: the text that goes on screen, and when.

The model call is replaced by a fake that returns what a model returns - including fenced JSON, overlapping
chunks and nonsense - because the module's job is to be *strict about its output* whatever the reply looks like:
every cue inside the clip, none overlapping, none longer than a line, the highlight timed from the words.
"""

import json
import unittest
from types import SimpleNamespace

from clipbot import editcaptions as C
from clipbot import editspec as E
from clipbot import edittimeline as T
from clipbot.config import Settings
from clipbot.utils import StageError
from pathlib import Path


def words(*items):
    return [{"w": w, "s": s, "e": e, "vs": s, "ve": e} for w, s, e in items]


WORDS = words(("हम", 10.0, 10.3), ("पे", 10.35, 10.5), ("मॉर्टर", 10.55, 11.1), ("चालू", 11.15, 11.5), ("हो", 11.55, 11.7), ("गया", 11.75, 12.2),
              ("भाई", 13.5, 13.9), ("रुको", 14.0, 14.4))


class TestParseChunks(unittest.TestCase):
    def test_plain_json(self):
        ch = C.parse_chunks('{"chunks": [{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya"}]}', 8)
        self.assertEqual(ch, [{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya", "conf": 0.8}])      # no conf given: 0.8

    def test_fenced_and_wrapped_in_prose(self):
        raw = 'Here you go:\n```json\n{"chunks": [{"from": 0, "to": 1, "text": "hum pe"}]}\n```\nHope that helps.'
        self.assertEqual(C.parse_chunks(raw, 8)[0]["text"], "hum pe")

    def test_prose_after_the_object_is_ignored_even_when_it_has_braces(self):
        raw = ('{"chunks": [{"from": 0, "to": 1, "text": "hum pe"}]}' + chr(10) + chr(10)
               + "Wait, that is 2 words {fine}." + chr(10) + "Let me reconsider {again}.")
        self.assertEqual(C.parse_chunks(raw, 8)[0]["text"], "hum pe")

    def test_a_chunk_that_overlaps_the_last_is_cut_back_to_start_after_it(self):
        ch = C.parse_chunks({"chunks": [{"from": 0, "to": 3, "text": "a b"}, {"from": 2, "to": 5, "text": "c d"}]}, 8)
        self.assertEqual([(c["from"], c["to"]) for c in ch], [(0, 3), (4, 5)])

    def test_indices_past_the_words_are_clamped_and_empty_ranges_dropped(self):
        ch = C.parse_chunks({"chunks": [{"from": 6, "to": 99, "text": "x"}, {"from": 9, "to": 12, "text": "y"}]}, 8)
        self.assertEqual([(c["from"], c["to"]) for c in ch], [(6, 7)])

    def test_text_is_trimmed_to_a_line_at_a_word_boundary(self):
        long = " ".join(["mortar"] * 12)
        (ch,) = C.parse_chunks({"chunks": [{"from": 0, "to": 3, "text": long}]}, 8)
        self.assertLessEqual(len(ch["text"]), C.MAX_CHARS)
        self.assertFalse(ch["text"].endswith(" "))

    def test_garbage_is_a_stage_error_that_shows_the_reply(self):
        with self.assertRaises(StageError) as ctx:
            C.parse_chunks("I cannot help with that", 8)
        self.assertIn("not JSON", str(ctx.exception))
        with self.assertRaises(StageError):
            C.parse_chunks({"nope": 1}, 8)

    def test_malformed_chunks_are_skipped_not_fatal(self):
        ch = C.parse_chunks({"chunks": [{"from": "x"}, {"from": 0, "to": 1, "text": "ok"}, {}]}, 8)
        self.assertEqual(len(ch), 1)


class TestConfidence(unittest.TestCase):
    def test_conf_is_read_clamped_and_defaults_when_missing_or_nonsense(self):
        ch = C.parse_chunks({"chunks": [{"from": 0, "to": 1, "text": "a", "conf": 7}, {"from": 2, "to": 3, "text": "b", "conf": "high"},
                                        {"from": 4, "to": 5, "text": "c", "conf": -1}]}, 8)
        self.assertEqual([c["conf"] for c in ch], [1.0, 0.8, 0.0])

    def test_a_low_confidence_cue_is_marked_off_and_not_given_to_the_renderer(self):
        cues = C.cues_from_chunks([{"from": 0, "to": 2, "text": "hum pe mortar", "conf": 0.3}], WORDS, 9.0, 13.0)
        self.assertTrue(cues[0]["off"])
        spec = E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [{"id": "a", "src": "v", "in": 9.0, "out": 13.0}]})
        pieces = T.build_pieces(spec["clips"][0])
        self.assertEqual(C.to_output(cues, pieces, 4.0), [])

    def test_a_hand_edit_that_clears_off_puts_the_cue_back(self):
        cues = C.cues_from_chunks([{"from": 0, "to": 2, "text": "hum pe mortar", "conf": 0.3}], WORDS, 9.0, 13.0)
        cues[0].pop("off")
        spec = E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [{"id": "a", "src": "v", "in": 9.0, "out": 13.0}]})
        self.assertEqual(len(C.to_output(cues, T.build_pieces(spec["clips"][0]), 4.0)), 1)

    def test_a_word_the_recogniser_stretched_over_a_pause_does_not_hold_the_cue_or_its_highlight(self):
        ws = words(("hain", 10.0, 10.3), ("bhai", 10.4, 14.0))                # "bhai" held for 3.6 s
        (cue,) = C.cues_from_chunks([{"from": 0, "to": 1, "text": "hain bhai", "conf": 1.0}], ws, 9.0, 20.0)
        self.assertLess(cue["e"], 10.4 + C.MAX_WORD + C.LINGER + 0.01)
        self.assertLessEqual(cue["words"][1]["e"] - cue["words"][1]["s"], C.MAX_WORD + 1e-6)


class TestFallback(unittest.TestCase):
    def test_breaks_at_pauses_and_at_six_words(self):
        ch = C.fallback_chunks(WORDS)
        self.assertGreaterEqual(len(ch), 2)                             # the 1.3 s pause before "भाई" is a boundary
        self.assertTrue(all(c["to"] - c["from"] <= 5 for c in ch))
        self.assertTrue(any(c["from"] == 6 for c in ch))
        self.assertTrue(all(len(c["text"]) <= C.MAX_CHARS for c in ch))

    def test_covers_every_word_once_in_order(self):
        ch = C.fallback_chunks(WORDS)
        idx = [i for c in ch for i in range(c["from"], c["to"] + 1)]
        self.assertEqual(idx, list(range(len(WORDS))))


class TestCues(unittest.TestCase):
    def test_a_cue_runs_from_its_first_word_to_its_last_plus_a_linger(self):
        (cue,) = C.cues_from_chunks([{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya"}], WORDS, 9.0, 13.0)
        self.assertEqual(cue["s"], 10.0)
        self.assertAlmostEqual(cue["e"], 12.2 + C.LINGER, places=3)

    def test_the_highlight_follows_the_recognisers_words_when_the_counts_agree(self):
        (cue,) = C.cues_from_chunks([{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya"}], WORDS, 9.0, 13.0)
        self.assertEqual([w["t"] for w in cue["words"]], ["hum", "pe", "mortar", "chalu", "ho", "gaya"])
        self.assertEqual(cue["words"][2]["s"], 10.55)
        self.assertTrue(all(a["e"] <= b["s"] + 1e-6 for a, b in zip(cue["words"], cue["words"][1:])))   # a word holds until the next

    def test_the_highlight_is_spread_by_length_when_the_counts_differ(self):
        (cue,) = C.cues_from_chunks([{"from": 0, "to": 5, "text": "hum pe mortar chalu"}], WORDS, 9.0, 13.0)
        self.assertEqual(len(cue["words"]), 4)
        self.assertAlmostEqual(cue["words"][0]["s"], cue["s"], places=3)
        spans = [w["e"] - w["s"] for w in cue["words"]]
        self.assertGreater(spans[2], spans[1])                          # "mortar" is longer than "pe"

    def test_a_one_word_cue_stays_long_enough_to_read(self):
        (cue,) = C.cues_from_chunks([{"from": 6, "to": 6, "text": "bhai"}], WORDS, 9.0, 20.0)
        self.assertGreaterEqual(cue["e"] - cue["s"], C.MIN_CUE)

    def test_cues_never_overlap(self):
        cues = C.cues_from_chunks([{"from": 0, "to": 2, "text": "hum pe mortar"}, {"from": 3, "to": 5, "text": "chalu ho gaya"}], WORDS, 9.0, 13.0)
        self.assertLessEqual(cues[0]["e"], cues[1]["s"])

    def test_the_part_of_a_cue_outside_the_clip_is_cut_and_cues_wholly_outside_are_dropped(self):
        cues = C.cues_from_chunks([{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya"}, {"from": 6, "to": 7, "text": "bhai ruko"}], WORDS, 10.5, 12.0)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["s"], 10.5)
        self.assertEqual(cues[0]["e"], 12.0)

    def test_an_empty_text_chunk_is_noise_and_has_no_cue(self):
        self.assertEqual(C.cues_from_chunks([{"from": 0, "to": 2, "text": ""}], WORDS, 9.0, 13.0), [])

    def test_silent_words_do_not_set_a_cues_times(self):
        ws = words(("a", 5.0, 5.3), ("b", 6.0, 6.3), ("c", 6.4, 6.8))
        ws[0]["silent"] = True
        (cue,) = C.cues_from_chunks([{"from": 0, "to": 2, "text": "x y"}], ws, 0.0, 20.0)
        self.assertEqual(cue["s"], 6.0)


class TestCensor(unittest.TestCase):
    def test_vowels_of_strong_words_are_masked_and_the_rest_is_left(self):
        self.assertEqual(C.censor("what the fuck"), "what the f*ck")
        self.assertEqual(C.censor("bhai chutiya hai"), "bhai ch*t*y* hai")

    def test_clean_text_is_untouched(self):
        self.assertEqual(C.censor("hum pe mortar chalu ho gaya"), "hum pe mortar chalu ho gaya")

    def test_a_strong_word_inside_an_ordinary_word_or_a_name_is_left_alone(self):
        self.assertEqual(C.censor("my friend Brandi jumped with a parachute"), "my friend Brandi jumped with a parachute")
        self.assertEqual(C.censor("Brandi, you bastard"), "Brandi, you b*st*rd")


class TestToOutput(unittest.TestCase):
    def pieces(self, clip):
        return T.build_pieces(E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [clip]})["clips"][0])

    def cue(self, s, e):
        return {"s": s, "e": e, "text": "x y", "words": [{"t": "x", "s": s, "e": (s + e) / 2}, {"t": "y", "s": (s + e) / 2, "e": e}]}

    def test_a_plain_clip_shifts_by_its_in_point(self):
        pieces = self.pieces({"id": "a", "src": "v", "in": 100.0, "out": 110.0})
        (c,) = C.to_output([self.cue(102.0, 104.0)], pieces, 10.0)
        self.assertEqual((c["s"], c["e"]), (2.0, 4.0))
        self.assertEqual(c["words"][1]["s"], 3.0)

    def test_a_slow_motion_ramp_stretches_the_cue_it_covers(self):
        clip = {"id": "a", "src": "v", "in": 0.0, "out": 10.0,
                "speed": [{"at": 4.0, "rate": 1.0}, {"at": 5.0, "rate": 0.5}, {"at": 6.0, "rate": 0.5}, {"at": 7.0, "rate": 1.0}]}
        pieces = self.pieces(clip)
        (c,) = C.to_output([self.cue(5.0, 6.0)], pieces, 12.0)
        self.assertGreater(c["e"] - c["s"], 1.5)                         # a second of source at half speed is two on screen

    def test_times_are_clamped_to_the_clip(self):
        pieces = self.pieces({"id": "a", "src": "v", "in": 0.0, "out": 5.0})
        (c,) = C.to_output([self.cue(4.0, 9.0)], pieces, 5.0)
        self.assertLessEqual(c["e"], 5.0)

    def test_a_cue_squeezed_to_nothing_is_dropped(self):
        pieces = self.pieces({"id": "a", "src": "v", "in": 0.0, "out": 5.0})
        self.assertEqual(C.to_output([self.cue(4.95, 5.0)], pieces, 5.0), [])


class TestAss(unittest.TestCase):
    cues = [{"s": 0.5, "e": 3.5, "text": "hum pe mortar", "words": [{"t": "hum", "s": 0.5, "e": 1.1}, {"t": "pe", "s": 1.1, "e": 1.4},
                                                                     {"t": "mortar", "s": 1.6, "e": 3.0}]}]

    def doc(self, **style):
        return C.ass_document(self.cues, dict({"highlight": "#35D0FF"}, **style), (1920, 1080), "Anton")

    def test_the_header_sets_the_canvas_the_font_and_a_box_left_of_centre(self):
        d = self.doc()
        self.assertIn("PlayResX: 1920", d)
        self.assertIn("PlayResY: 1080", d)
        self.assertIn("Style: Cap,Anton,63,", d)                          # 0.058 of 1080, rounded
        # margins: box centre 0.42, width 0.52 -> left 0.16 (307 px), right 1 - 0.68 (614 px)
        self.assertIn(",2,307,614,", d)

    def test_karaoke_gives_each_word_its_duration_and_a_beat_for_a_gap(self):
        d = self.doc()
        line = [l for l in d.splitlines() if l.startswith("Dialogue")][0]
        self.assertIn("{\\k60}hum", line)
        self.assertIn("{\\k30}pe", line)
        self.assertIn("{\\k20}", line)                                    # the 0.2 s gap before "mortar"
        self.assertIn("{\\k140}mortar", line)
        self.assertIn("0:00:00.50,0:00:03.50", line)

    def test_the_spoken_word_takes_the_highlight_colour_and_the_rest_the_text_colour(self):
        d = self.doc(highlight="#35D0FF", color="#FFFFFF")
        style = [l for l in d.splitlines() if l.startswith("Style:")][0]
        self.assertIn("&H00FFD035,&H00FFFFFF", style)                     # primary = highlight (BGR), secondary = text

    def test_without_karaoke_the_whole_line_is_one_colour_and_has_no_k_tags(self):
        d = self.doc(karaoke=False)
        self.assertNotIn("\\k", d)
        self.assertIn("hum pe mortar", d)

    def test_strong_words_are_masked_unless_censoring_is_off(self):
        cue = [{"s": 0.0, "e": 2.0, "text": "what the fuck", "words": [{"t": "what", "s": 0.0, "e": 0.4}, {"t": "fuck", "s": 0.5, "e": 1.0}]}]
        self.assertIn("f*ck", C.ass_document(cue, {}, (1920, 1080), "Anton"))
        self.assertIn("fuck", C.ass_document(cue, {"censor": False}, (1920, 1080), "Anton"))

    def test_braces_and_backslashes_in_text_cannot_inject_override_tags(self):
        cue = [{"s": 0.0, "e": 2.0, "text": "{\\an8}hi", "words": [{"t": "{\\an8}hi", "s": 0.0, "e": 1.0}]}]
        d = C.ass_document(cue, {}, (1920, 1080), "Anton")
        self.assertNotIn("{\\an8}", d)

    def test_times_format(self):
        self.assertEqual(C._ass_time(3725.456), "1:02:05.46")
        self.assertEqual(C._ass_time(0.0), "0:00:00.00")


class FakeModel:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, model=None, max_tokens=None, messages=None):
        self.calls.append((model, messages[0]["content"]))
        r = self.replies.pop(0) if self.replies else self.replies_default
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(text=r)])

    replies_default = '{"chunks": []}'


class TestGenerate(unittest.TestCase):
    settings = Settings({"work_root": "D:/w"}, Path("s.json"))

    def clips(self):
        return {"a": {"words": WORDS, "in": 9.0, "out": 13.0}, "b": {"words": WORDS, "in": 13.0, "out": 15.0}}

    def good(self):
        return json.dumps({"chunks": [{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya"}, {"from": 6, "to": 7, "text": "bhai ruko"}]})

    def test_each_clip_gets_a_call_with_its_words_and_the_models_text_becomes_its_cues(self):
        client = FakeModel([self.good(), self.good()])
        doc = C.generate(self.settings, self.clips(), client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0][0], "claude-sonnet-5-5")
        self.assertIn("0\t10.00\tहम", client.calls[0][1])
        self.assertEqual(doc["clips"]["a"]["cues"][0]["text"], "hum pe mortar chalu ho gaya")
        self.assertFalse(doc["clips"]["a"]["fallback"])
        self.assertEqual(doc["version"], C.SCHEMA)

    def test_a_clip_whose_model_call_keeps_failing_falls_back_and_the_others_are_unaffected(self):
        boom = RuntimeError("overloaded")
        client = FakeModel([boom, boom, self.good()])          # clip a: two failures; clip b: fine (order of completion may vary)
        doc = C.generate(Settings({"work_root": "D:/w", "captions": {"concurrency": 1}}, Path("s.json")), self.clips(), client=client)
        self.assertTrue(doc["clips"]["a"]["fallback"])
        self.assertTrue(doc["clips"]["a"]["cues"])             # the recogniser's own text still gives cues
        self.assertFalse(doc["clips"]["b"]["fallback"])

    def test_an_unreadable_reply_gets_one_retry_then_falls_back(self):
        client = FakeModel(["not json", "still not json"])
        doc = C.generate(Settings({"work_root": "D:/w", "captions": {"concurrency": 1}}, Path("s.json")), {"a": self.clips()["a"]}, client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(doc["clips"]["a"]["fallback"])

    def test_a_clip_with_no_words_has_no_cues_and_no_call(self):
        client = FakeModel([])
        doc = C.generate(self.settings, {"a": {"words": [], "in": 0.0, "out": 5.0}}, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(doc["clips"]["a"]["cues"], [])

    def test_the_caption_model_is_a_setting(self):
        client = FakeModel([self.good()])
        C.generate(Settings({"work_root": "D:/w", "captions": {"model": "claude-haiku-4-5"}}, Path("s.json")), {"a": self.clips()["a"]}, client=client)
        self.assertEqual(client.calls[0][0], "claude-haiku-4-5")

    def test_a_chunk_the_model_is_unsure_of_is_kept_in_the_file_but_marked_off(self):
        reply = json.dumps({"chunks": [{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya", "conf": 0.9},
                                       {"from": 6, "to": 7, "text": "daring times fishy", "conf": 0.2}]})
        doc = C.generate(self.settings, {"a": dict(self.clips()["a"], out=15.0)}, client=FakeModel([reply]))
        cues = doc["clips"]["a"]["cues"]
        self.assertEqual(len(cues), 2)
        self.assertNotIn("off", cues[0])
        self.assertTrue(cues[1]["off"])
        self.assertEqual(cues[1]["conf"], 0.2)

    def test_the_threshold_is_a_setting(self):
        reply = json.dumps({"chunks": [{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya", "conf": 0.7}]})
        strict = C.generate(Settings({"work_root": "D:/w", "captions": {"min_confidence": 0.9}}, Path("s.json")), {"a": self.clips()["a"]}, client=FakeModel([reply]))
        lax = C.generate(Settings({"work_root": "D:/w", "captions": {"min_confidence": 0.5}}, Path("s.json")), {"a": self.clips()["a"]}, client=FakeModel([reply]))
        self.assertTrue(strict["clips"]["a"]["cues"][0].get("off"))
        self.assertFalse(lax["clips"]["a"]["cues"][0].get("off"))


if __name__ == "__main__":
    unittest.main()
