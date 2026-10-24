"""Tests for clipbot/editcli.py: the `clipbot edit` / `clipbot killfeed` command groups."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipbot import cli, editcli
from clipbot.config import Settings
from clipbot.utils import StageError


def run(handler, settings, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = handler(argparse.Namespace(**kw), settings)
    return code, out.getvalue()


class TestParser(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def test_groups_are_registered(self):
        self.assertEqual(self.parse("edit", "render", "reel", "--preview").func, editcli.cmd_edit_render)
        self.assertEqual(self.parse("edit", "new", "reel", "--killfeed", "ws1", "ws2").killfeed, ["ws1", "ws2"])
        self.assertEqual(self.parse("killfeed", "scan", "--workspace", "w", "--player", "Teck").player, "Teck")
        self.assertEqual(self.parse("edit", "taste", "add", "no whoosh", "--said", "ugh").said, "ugh")

    def test_defaults(self):
        a = self.parse("edit", "new", "reel", "--killfeed", "w")
        self.assertEqual((a.profile, a.order, a.slowmo, a.preset), ("punchy", "chapters", "nade", "youtube_1080p60"))

    def test_bad_choices_are_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.parse("edit", "new", "reel", "--killfeed", "w", "--profile", "loud")

    def test_audio_refs(self):
        self.assertEqual(editcli._audio_ref("sfx_08a073fd8c00c828"), {"asset": "sfx_08a073fd8c00c828"})
        self.assertEqual(editcli._audio_ref("mus_3a25c534c578d312"), {"asset": "mus_3a25c534c578d312"})
        self.assertIn("file", editcli._audio_ref("C:/sounds/hit.wav"))


class TestEditNew(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ws = self.root / "ws-one"
        (ws / "killfeed_icons").mkdir(parents=True)
        (ws / "killfeed_icons" / "k001.png").write_bytes(b"x")
        events = [
            {"id": "k001", "t": 300.0, "dist_m": 238.0, "weapon": "gun", "headshot": True, "through_cover": False,
             "victim": "KULLER", "icon": "killfeed_icons/k001.png"},
            {"id": "k002", "t": 900.0, "dist_m": 20.0, "weapon": "grenade", "headshot": False, "through_cover": False,
             "victim": "Nader", "icon": None},
            {"id": "k003", "t": 1500.0, "dist_m": 62.0, "weapon": "gun", "headshot": False, "through_cover": False,
             "victim": "X", "icon": None}]
        (ws / "killfeed.json").write_text(json.dumps({"player": "Teck", "kills": 3, "nades": 1, "headshots": 1, "events": events}),
                                          encoding="utf-8")
        self.settings = Settings({"work_root": str(self.root), "killfeed": {"player": "Teck"}}, Path("s.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kw):
        d = dict(name="reel", template="kill-montage", killfeed=["ws-one"], player=None, game="Wardogs", profile="punchy",
                 order="chapters", max_clips=None, slowmo="nade", grade="gamer_pop", preset="youtube_1080p60", music=None,
                 music_gain=-18.0, sfx_hit=None, sfx_nade=None, sfx_headshot=None, no_intro=False, no_outro=False, force=False)
        d.update(kw)
        return d

    def test_writes_a_spec_and_facts_and_summarises(self):
        code, out = run(editcli.cmd_edit_new, self.settings, **self.args())
        self.assertEqual(code, 0)
        spec = json.loads((self.root / "_edits" / "reel" / "spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["sources"]["v1"], {"workspace": "ws-one"})
        facts = json.loads((self.root / "_edits" / "reel" / "facts.json").read_text(encoding="utf-8"))
        self.assertEqual((facts["kills"], facts["nades"], facts["far"], facts["player"]), (3, 1, 238.0, "Teck"))
        self.assertIn("clip(s)", out)
        self.assertIn("chapters:", out)

    def test_icons_become_absolute_paths_in_the_spec(self):
        run(editcli.cmd_edit_new, self.settings, **self.args())
        spec = json.loads((self.root / "_edits" / "reel" / "spec.json").read_text(encoding="utf-8"))
        images = [o.get("image") for c in spec["clips"] for o in c.get("overlays", []) if o.get("image")]
        self.assertEqual(len(images), 1)
        self.assertTrue(Path(images[0]).is_absolute())

    def test_an_existing_spec_is_protected_and_kept_on_force(self):
        run(editcli.cmd_edit_new, self.settings, **self.args())
        with self.assertRaises(StageError):
            run(editcli.cmd_edit_new, self.settings, **self.args())
        run(editcli.cmd_edit_new, self.settings, **self.args(force=True, profile="balanced"))
        root = self.root / "_edits" / "reel"
        self.assertEqual(json.loads((root / "spec.json").read_text(encoding="utf-8"))["profile"], "balanced")
        self.assertEqual(json.loads((root / "spec.previous.json").read_text(encoding="utf-8"))["profile"], "punchy")

    def test_a_workspace_without_a_scan_says_how_to_make_one(self):
        (self.root / "ws-two").mkdir()
        with self.assertRaises(StageError) as cm:
            run(editcli.cmd_edit_new, self.settings, **self.args(killfeed=["ws-two"]))
        self.assertIn("killfeed scan", str(cm.exception))

    def test_unknown_workspace(self):
        with self.assertRaises(StageError):
            run(editcli.cmd_edit_new, self.settings, **self.args(killfeed=["nope"]))

    def test_validate_and_inspect_an_edit_by_name(self):
        run(editcli.cmd_edit_new, self.settings, **self.args())
        code, out = run(editcli.cmd_edit_validate, self.settings, spec="reel")
        self.assertEqual(code, 0)
        self.assertIn("pacing:", out)
        code, out = run(editcli.cmd_edit_inspect, self.settings, spec="reel")
        self.assertIn("intro", out)
        self.assertIn("k01", out)

    def test_validate_reports_a_bad_spec_instead_of_a_traceback(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"name": "t", "sources": {}, "clips": []}), encoding="utf-8")
        code, out = run(editcli.cmd_edit_validate, self.settings, spec=str(bad))
        self.assertEqual(code, 1)
        self.assertIn("INVALID", out)

    def test_unknown_edit_name(self):
        with self.assertRaises(StageError):
            run(editcli.cmd_edit_validate, self.settings, spec="no-such-edit")

    def test_list_and_clean(self):
        code, out = run(editcli.cmd_edit_list, self.settings)
        self.assertIn("no edits yet", out)
        run(editcli.cmd_edit_new, self.settings, **self.args())
        cache = self.root / "_edits" / "reel" / "cache" / "final"
        cache.mkdir(parents=True)
        (cache / "x.bin").write_bytes(b"0" * 2048)
        code, out = run(editcli.cmd_edit_list, self.settings)
        self.assertIn("reel", out)
        self.assertIn("not rendered", out)
        code, out = run(editcli.cmd_edit_clean, self.settings, spec="reel", segments=False)
        self.assertIn("freed", out)
        self.assertFalse((self.root / "_edits" / "reel" / "cache").exists())
        self.assertTrue((self.root / "_edits" / "reel" / "spec.json").exists())      # the spec stays

    def test_verify_and_deliver_need_a_render(self):
        run(editcli.cmd_edit_new, self.settings, **self.args())
        with self.assertRaises(StageError) as cm:
            run(editcli.cmd_edit_verify, self.settings, spec="reel", preview=False, shallow=False)
        self.assertIn("not rendered", str(cm.exception))


class TestTasteCommand(unittest.TestCase):
    def test_add_list_retire(self):
        with tempfile.TemporaryDirectory() as d:
            s = Settings({"work_root": d}, Path("s.json"))
            code, out = run(editcli.cmd_edit_taste, s, action="list", text=None, said=None, edit=None, all=False)
            self.assertIn("no taste rules", out)
            code, out = run(editcli.cmd_edit_taste, s, action="add", text="callouts bottom-left", said="not centre", edit=None, all=False)
            self.assertIn("t1:", out)
            code, out = run(editcli.cmd_edit_taste, s, action="list", text=None, said=None, edit=None, all=False)
            self.assertIn("callouts bottom-left", out)
            self.assertIn("not centre", out)
            run(editcli.cmd_edit_taste, s, action="retire", text="t1", said=None, edit=None, all=False)
            code, out = run(editcli.cmd_edit_taste, s, action="list", text=None, said=None, edit=None, all=False)
            self.assertIn("no taste rules", out)
            code, out = run(editcli.cmd_edit_taste, s, action="list", text=None, said=None, edit=None, all=True)
            self.assertIn("(retired)", out)

    def test_add_without_text(self):
        with tempfile.TemporaryDirectory() as d:
            s = Settings({"work_root": d}, Path("s.json"))
            with self.assertRaises(StageError):
                run(editcli.cmd_edit_taste, s, action="add", text=None, said=None, edit=None, all=False)


class TestEditEpisode(unittest.TestCase):
    """`clipbot edit episode`: a lineup file -> snapped cuts, matched levels, spec + lineup + cuts + facts."""

    def setUp(self):
        import wave

        import numpy as np

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ws = self.root / "ws-one"
        ws.mkdir()
        rate = 16000
        t = np.arange(rate * 6) / float(rate)
        speech = 0.3 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 5 * t))
        gap = np.random.RandomState(1).randn(rate * 2) * 0.001
        pcm = (np.concatenate([speech, gap, speech, gap, speech]) * 32767).astype("<i2")
        with wave.open(str(ws / "audio.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        (ws / "state.json").write_text("{}", encoding="utf-8")
        self.settings = Settings({"work_root": str(self.root)}, Path("s.json"))
        self.lineup = {
            "sources": {"a": {"workspace": "ws-one"}, "f": {"file": str(self.root / "x.mp4")}},
            "cold_open": {"title": "Cold open", "whoosh": False, "clips": [{"src": "a", "in": 8.3, "out": 13.7}]},
            "sting": {"dur": 3.0},
            "chapters": [{"title": "One", "clips": [{"src": "a", "in": 8.3, "out": 13.7}, {"src": "f", "in": 2.0, "out": 6.0}]},
                         {"title": "Two", "clips": [{"src": "a", "in": 9.0, "out": 12.0}]},
                         {"title": "Three", "clips": [{"src": "a", "in": 1.0, "out": 5.0}]}],
            "end": {"dur": 5.0},
            "titles": ["EP #1 - [TITLE TBD]"], "fact_lines": ["A roundup."], "credits": ["Streams:"], "notes": ["720p upscaled"],
        }
        self.file = self.root / "lineup.json"
        self.file.write_text(json.dumps(self.lineup), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kw):
        d = dict(name="ep", lineup=str(self.file), no_snap=False, no_match=False, force=False)
        d.update(kw)
        return d

    def test_builds_everything(self):
        code, out = run(editcli.cmd_edit_episode, self.settings, **self.args())
        self.assertEqual(code, 0)
        root = self.root / "_edits" / "ep"
        for name in ("spec.json", "lineup.json", "cuts.json", "facts.json"):
            self.assertTrue((root / name).is_file(), name)
        self.assertIn("clip(s) moved", out)
        self.assertIn("chapters:", out)
        spec = json.loads((root / "spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["name"], "ep")                                   # the command's name wins
        facts = json.loads((root / "facts.json").read_text(encoding="utf-8"))
        self.assertEqual(facts["titles"], ["EP #1 - [TITLE TBD]"])
        self.assertEqual(facts["notes"], ["720p upscaled"])
        self.assertEqual(facts["clips"], 5)

    def test_the_prepared_lineup_records_where_the_cuts_went(self):
        run(editcli.cmd_edit_episode, self.settings, **self.args())
        root = self.root / "_edits" / "ep"
        prepared = json.loads((root / "lineup.json").read_text(encoding="utf-8"))
        cold = prepared["cold_open"]["clips"][0]
        self.assertNotEqual((cold["in"], cold["out"]), (8.3, 13.7))          # moved into the gaps at 6-8 s and 14-16 s
        cuts = json.loads((root / "cuts.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cuts["clips"]), 5)
        self.assertEqual(cuts["clips"][0]["in_was"], 8.3)

    def test_a_file_source_is_left_as_authored(self):
        run(editcli.cmd_edit_episode, self.settings, **self.args())
        prepared = json.loads((self.root / "_edits" / "ep" / "lineup.json").read_text(encoding="utf-8"))
        f = prepared["chapters"][0]["clips"][1]
        self.assertEqual((f["in"], f["out"]), (2.0, 6.0))
        self.assertNotIn("gain_db", f)

    def test_no_snap_keeps_the_authors_times(self):
        run(editcli.cmd_edit_episode, self.settings, **self.args(no_snap=True))
        prepared = json.loads((self.root / "_edits" / "ep" / "lineup.json").read_text(encoding="utf-8"))
        cold = prepared["cold_open"]["clips"][0]
        self.assertEqual((cold["in"], cold["out"]), (8.3, 13.7))

    def test_refuses_to_overwrite_without_force_and_keeps_the_old_spec(self):
        run(editcli.cmd_edit_episode, self.settings, **self.args())
        with self.assertRaises(StageError):
            run(editcli.cmd_edit_episode, self.settings, **self.args())
        run(editcli.cmd_edit_episode, self.settings, **self.args(force=True))
        self.assertTrue((self.root / "_edits" / "ep" / "spec.previous.json").is_file())

    def test_parser(self):
        a = cli.build_parser().parse_args(["edit", "episode", "ep", "lineup.json", "--no-snap"])
        self.assertEqual((a.func, a.no_snap, a.no_match), (editcli.cmd_edit_episode, True, False))


class TestWordsAndCaptions(TestEditEpisode):
    """`edit episode --words`, `edit words`, `edit captions`: the word-timing and caption commands, with the
    network calls (OpenAI word timing, the caption model) replaced by fakes that record what they were asked."""

    @staticmethod
    def fake_words(settings, ws, a, b, fetch=True, **kw):
        out, t, i = [], a - 1.0, 0
        while t < b + 1.0:
            out.append({"w": "w{0}".format(i), "s": round(t, 3), "e": round(t + 0.4, 3), "vs": round(t, 3), "ve": round(t + 0.4, 3)})
            t += 0.5
            i += 1
        return out

    def build(self):
        run(editcli.cmd_edit_episode, self.settings, **self.args())
        p = self.root / "_edits" / "ep" / "spec.json"           # captions on, so the render path is the one a real episode takes
        spec = json.loads(p.read_text(encoding="utf-8"))
        spec["captions"] = {"highlight": "#35D0FF"}
        p.write_text(json.dumps(spec), encoding="utf-8")

    def test_episode_words_moves_a_cut_that_split_a_word_and_says_what_it_fetched(self):
        words = [{"w": "bolna", "s": 8.0, "e": 9.0, "vs": 8.0, "ve": 9.0}]
        with mock.patch.object(editcli.editwords, "words_for", return_value=words) as m:
            code, out = run(editcli.cmd_edit_episode, self.settings, **self.args(no_snap=False, words=True))
        self.assertEqual(code, 0)
        self.assertIn("OpenAI whisper-1", out)
        self.assertTrue(m.called)
        prepared = json.loads((self.root / "_edits" / "ep" / "lineup.json").read_text(encoding="utf-8"))
        self.assertLess(prepared["cold_open"]["clips"][0]["in"], 8.0)

    def test_edit_words_fetches_every_clip_with_a_workspace_and_reports_the_cuts_it_would_move(self):
        self.build()
        with mock.patch.object(editcli.editwords, "words_for", side_effect=self.fake_words) as m:
            code, out = run(editcli.cmd_edit_words, self.settings, spec="ep", check_only=False)
        self.assertEqual(code, 0)
        self.assertIn("about $", out)
        self.assertIn("words in", out)
        self.assertIn("cut point(s) land inside a word", out)
        self.assertEqual(m.call_count, 4)                      # the four clips that read ws-one; the file source is skipped
        words = json.loads((self.root / "_edits" / "ep" / "words.json").read_text(encoding="utf-8"))
        self.assertEqual(len(words["clips"]), 4)

    def test_a_clip_the_recogniser_heard_nothing_in_is_named_and_not_confused_with_one_not_fetched(self):
        self.build()
        calls = []

        def words(settings, ws, a, b, fetch=True, **kw):
            calls.append(a)
            return [] if len(calls) % 4 == 1 else self.fake_words(settings, ws, a, b)

        with mock.patch.object(editcli.editwords, "words_for", side_effect=words):
            code, out = run(editcli.cmd_edit_words, self.settings, spec="ep", check_only=False)
        self.assertIn("NO WORDS HEARD in", out)
        self.assertEqual(out.count("NO WORDS HEARD in"), 1)
        client, _ = self.fake_model()

        def stored(settings, ws, a, b, fetch=True, **kw):          # captions reads the store: [] = heard none, None = never fetched
            return [] if a == calls[0] else self.fake_words(settings, ws, a, b)

        with mock.patch.object(editcli.editwords, "words_for", side_effect=stored), \
                mock.patch.object(editcli.editcaptions, "_client", return_value=client):
            code, out = run(editcli.cmd_edit_captions, self.settings, spec="ep", force=True, clips=None, no_model=False)
        self.assertIn("no speech heard", out)
        self.assertNotIn("no words stored", out)

    def test_check_only_sends_nothing(self):
        self.build()
        with mock.patch.object(editcli.editwords, "words_for", return_value=None) as m:
            run(editcli.cmd_edit_words, self.settings, spec="ep", check_only=True)
        self.assertTrue(all(call.kwargs.get("fetch") is False for call in m.call_args_list))

    def fake_model(self):
        calls = []

        def create(model=None, max_tokens=None, messages=None):
            calls.append(model)
            listing = messages[0]["content"].split("Words (index, start second, word):\n")[1]
            n = len([line for line in listing.splitlines() if line.strip()])
            chunks = [{"from": i, "to": min(i + 2, n - 1), "text": "hello w{0}".format(i)} for i in range(0, n, 3)]
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"chunks": chunks}))])

        return SimpleNamespace(messages=SimpleNamespace(create=create)), calls

    def test_edit_captions_writes_captions_json_and_will_not_overwrite_it_without_force(self):
        self.build()
        client, calls = self.fake_model()
        with mock.patch.object(editcli.editwords, "words_for", side_effect=self.fake_words), \
                mock.patch.object(editcli.editcaptions, "_client", return_value=client):
            code, out = run(editcli.cmd_edit_captions, self.settings, spec="ep", force=False, clips=None, no_model=False)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 4)
            doc = json.loads((self.root / "_edits" / "ep" / "captions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(doc["clips"]), 4)
            self.assertTrue(all(v["cues"] for v in doc["clips"].values()))
            self.assertIn("no strong language", out)
            with self.assertRaises(StageError):
                run(editcli.cmd_edit_captions, self.settings, spec="ep", force=False, clips=None, no_model=False)

    def test_clips_redoes_only_those_and_keeps_the_rest_including_hand_edits(self):
        self.build()
        client, calls = self.fake_model()
        with mock.patch.object(editcli.editwords, "words_for", side_effect=self.fake_words), \
                mock.patch.object(editcli.editcaptions, "_client", return_value=client):
            run(editcli.cmd_edit_captions, self.settings, spec="ep", force=False, clips=None, no_model=False)
            path = self.root / "_edits" / "ep" / "captions.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            first, second = list(doc["clips"])[:2]
            doc["clips"][second]["cues"][0]["text"] = "MY EDIT"
            path.write_text(json.dumps(doc), encoding="utf-8")
            calls.clear()
            run(editcli.cmd_edit_captions, self.settings, spec="ep", force=False, clips=first, no_model=False)
        self.assertEqual(len(calls), 1)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["clips"][second]["cues"][0]["text"], "MY EDIT")

    def test_no_model_uses_the_recognisers_own_text_and_says_so(self):
        self.build()
        with mock.patch.object(editcli.editwords, "words_for", side_effect=self.fake_words):
            code, out = run(editcli.cmd_edit_captions, self.settings, spec="ep", force=False, clips=None, no_model=True)
        self.assertEqual(code, 0)
        doc = json.loads((self.root / "_edits" / "ep" / "captions.json").read_text(encoding="utf-8"))
        self.assertTrue(all(v["fallback"] for v in doc["clips"].values()))

    def test_strong_language_in_the_first_thirty_seconds_is_called_out(self):
        self.build()

        def create(model=None, max_tokens=None, messages=None):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"chunks": [{"from": 0, "to": 3, "text": "what the fuck"}]}))])

        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with mock.patch.object(editcli.editwords, "words_for", side_effect=self.fake_words), \
                mock.patch.object(editcli.editcaptions, "_client", return_value=client):
            code, out = run(editcli.cmd_edit_captions, self.settings, spec="ep", force=False, clips=None, no_model=False)
        self.assertIn("IN THE FIRST 30 s", out)

    def test_parsers(self):
        a = cli.build_parser().parse_args(["edit", "captions", "ep", "--clips", "a,b", "--no-model"])
        self.assertEqual((a.func, a.clips, a.no_model), (editcli.cmd_edit_captions, "a,b", True))
        b = cli.build_parser().parse_args(["edit", "words", "ep", "--check-only"])
        self.assertEqual((b.func, b.check_only), (editcli.cmd_edit_words, True))
        c = cli.build_parser().parse_args(["edit", "episode", "ep", "l.json", "--words"])
        self.assertTrue(c.words)


if __name__ == "__main__":
    unittest.main()
