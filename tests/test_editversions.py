"""editversions.py: the live final is archived when the next one is complete, numbered, and described - with no ffmpeg."""

import json
import tempfile
import unittest
from pathlib import Path

from clipbot import editversions as V


def spec(*ids, profile="restrained"):
    return {"profile": profile, "output": {"width": 1920, "height": 1080, "fps": 60}, "master": {}, "captions": None,
            "tracks": {"music": [], "sfx": []},
            "clips": [{"id": i, "type": "clip", "label": i.upper(), "src": "v", "in": 1.0, "out": 5.0, "audio": {"gain_db": 0.0}} for i in ids]}


class Edit:
    """A throwaway edit folder with a fake render pipeline: `render()` does what render_edit does around the mux."""

    def __init__(self, tmp, name="e"):
        self.root = Path(tmp) / name
        self.name = name
        (self.root / "render").mkdir(parents=True)

    def final(self):
        return self.root / "render" / (self.name + ".mp4")

    def put_live(self, body=b"live", spec_obj=None, n_files=True):
        self.final().write_bytes(body)
        if spec_obj is not None:
            (self.root / "render" / (self.name + ".final.spec.json")).write_text(json.dumps(spec_obj), encoding="utf-8")
        (self.root / "render" / (self.name + ".final.timeline.json")).write_text(json.dumps({"timeline": {"clips": []}, "report": {"duration": 12.5}}), encoding="utf-8")

    def render(self, new_spec, body=b"new"):
        prev = V.begin_final(self.root, self.name)
        # render_edit overwrites the spec record straight away
        (self.root / "render" / (self.name + ".final.spec.json")).write_text(json.dumps(new_spec), encoding="utf-8")
        part = self.final().with_name(self.name + ".part.mp4")
        part.write_bytes(body)
        return V.commit_final(self.root, self.name, prev, new_spec, {"duration": 20.0, "bytes": len(body)}, part, self.final(), now=0)


class TestNumbering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.e = Edit(self._tmp.name)

    def test_the_first_render_is_version_one_and_archives_nothing(self):
        entry = self.e.render(spec("a", "b"))
        self.assertEqual((entry["n"], entry["from"], entry["changes"]), (1, None, ["first render: 2 clip(s)"]))
        self.assertEqual(V.archived_numbers(self.e.root), [])
        self.assertEqual(self.e.final().read_bytes(), b"new")
        self.assertEqual(V.load(self.e.root)["current"], 1)

    def test_each_render_archives_the_one_before_and_says_what_changed(self):
        self.e.render(spec("a", "b"), b"one")
        entry = self.e.render(spec("a", "b", "c"), b"two")
        self.assertEqual((entry["n"], entry["from"]), (2, 1))
        self.assertEqual(entry["changes"], ["added c (C)"])
        archive = self.e.root / "render" / "v1"
        self.assertEqual((archive / "e.v1.mp4").read_bytes(), b"one")
        self.assertEqual(json.loads((archive / "e.v1.spec.json").read_text(encoding="utf-8"))["clips"][0]["id"], "a")
        self.assertEqual(len(json.loads((archive / "e.v1.spec.json").read_text(encoding="utf-8"))["clips"]), 2)       # the OLD spec, not the new
        self.assertEqual(self.e.final().read_bytes(), b"two")
        third = self.e.render(spec("a", "c"), b"three")
        self.assertEqual((third["n"], third["from"], third["changes"]), (3, 2, ["removed b (B)"]))
        self.assertEqual(V.archived_numbers(self.e.root), [1, 2])
        self.assertEqual(V.live_number(self.e.root, "e"), 3)

    def test_the_changelog_lists_versions_newest_first_in_words(self):
        self.e.render(spec("a"))
        self.e.render(spec("a", profile="punchy"))
        text = (self.e.root / "changelog.md").read_text(encoding="utf-8")
        self.assertLess(text.index("## v2"), text.index("## v1"))
        self.assertIn("- profile restrained -> punchy", text)
        self.assertIn("from v1:", text)

    def test_a_lineup_and_captions_are_kept_with_the_version_they_belonged_to(self):
        (self.e.root / "lineup.json").write_text('{"name": "e"}', encoding="utf-8")
        (self.e.root / "captions.json").write_text('{"clips": {}}', encoding="utf-8")
        self.e.render(spec("a"))
        self.e.render(spec("a", "b"))
        self.assertTrue((self.e.root / "render" / "v1" / "lineup.v1.json").exists())
        self.assertTrue((self.e.root / "render" / "v1" / "captions.v1.json").exists())


class TestLegacy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.e = Edit(self._tmp.name)

    def archive_by_hand(self, *ns):
        for n in ns:
            d = self.e.root / "render" / "v{0}".format(n)
            d.mkdir()
            (d / "e.v{0}.mp4".format(n)).write_bytes(b"hand" + bytes([n]))

    def test_an_edit_archived_by_hand_has_its_live_final_numbered_after_the_last(self):
        self.archive_by_hand(1, 2, 4)
        self.e.put_live(b"v5body", spec("a"))
        self.assertEqual(V.live_number(self.e.root, "e"), 5)
        entry = self.e.render(spec("a", "b"), b"v6body")
        self.assertEqual((entry["n"], entry["from"]), (6, 5))
        self.assertEqual((self.e.root / "render" / "v5" / "e.v5.mp4").read_bytes(), b"v5body")
        self.assertEqual((self.e.root / "render" / "v4" / "e.v4.mp4").read_bytes(), b"hand\x04")       # the hand-made ones are untouched

    def test_a_taken_archive_number_is_skipped_never_overwritten(self):
        self.archive_by_hand(1)
        self.e.put_live(b"live", spec("a"))
        (self.e.root / "versions.json").write_text(json.dumps({"current": 1, "history": []}), encoding="utf-8")      # says live is v1: so is the archive
        entry = self.e.render(spec("a", "b"))
        self.assertEqual((self.e.root / "render" / "v1" / "e.v1.mp4").read_bytes(), b"hand\x01")
        self.assertEqual(sorted(V.archived_numbers(self.e.root)), [1, 2])
        self.assertEqual(entry["n"], 3)

    def test_a_live_final_with_no_recorded_spec_is_archived_and_the_new_one_reads_as_a_first_render(self):
        self.e.put_live(b"old")                       # no final.spec.json
        entry = self.e.render(spec("a"))
        self.assertEqual(entry["changes"], ["first render: 1 clip(s)"])
        self.assertEqual((self.e.root / "render" / "v1" / "e.v1.mp4").read_bytes(), b"old")
        self.assertFalse((self.e.root / "render" / "v1" / "e.v1.spec.json").exists())


class TestFailureAndListing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.e = Edit(self._tmp.name)

    def test_beginning_a_render_that_then_fails_changes_nothing_but_the_spec_record(self):
        self.e.render(spec("a"), b"one")
        before = V.load(self.e.root)
        V.begin_final(self.e.root, "e")               # ... and the render dies before commit_final
        self.assertEqual(self.e.final().read_bytes(), b"one")
        self.assertEqual(V.archived_numbers(self.e.root), [])
        self.assertEqual(V.load(self.e.root), before)

    def test_the_versions_that_can_be_played_are_listed_oldest_first_with_the_preview_last(self):
        self.e.render(spec("a"), b"one")
        self.e.render(spec("a", "b"), b"two")
        (self.e.root / "render" / "e.preview.mp4").write_bytes(b"pv")
        vs = V.list_versions(self.e.root, "e")
        self.assertEqual([v["which"] for v in vs], ["v1", "final", "preview"])
        self.assertEqual([v["n"] for v in vs], [1, 2, None])
        self.assertEqual(vs[1]["summary"], "+1 clips")
        self.assertEqual(V.resolve_file(self.e.root, "e", "v1").read_bytes(), b"one")
        self.assertEqual(V.resolve_file(self.e.root, "e", "final").read_bytes(), b"two")
        self.assertIsNone(V.resolve_file(self.e.root, "e", "v7"))
        self.assertIsNone(V.resolve_file(self.e.root, "e", "../../etc"))

    def test_no_renders_is_no_versions(self):
        self.assertEqual(V.list_versions(self.e.root, "e"), [])
        self.assertIsNone(V.live_number(self.e.root, "e"))

    def test_a_corrupt_versions_file_is_treated_as_none_not_a_crash(self):
        (self.e.root / "versions.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(V.load(self.e.root), {"current": None, "history": []})


if __name__ == "__main__":
    unittest.main()
