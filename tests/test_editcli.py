"""Tests for clipbot/editcli.py: the `clipbot edit` / `clipbot killfeed` command groups."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
