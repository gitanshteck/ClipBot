"""Tests for clipbot/editdeliver.py: chapters, thumbnails, variants, post text."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clipbot import editdeliver as D
from clipbot import editspec as E
from clipbot import edittimeline as T

SOURCES = {"v": {"file": "D:/x.mp4"}, "w": {"file": "D:/y.mp4"}}


def timeline(clips, **extra):
    return T.compile_timeline(E.normalize(dict({"name": "t", "sources": SOURCES, "clips": clips}, **extra)))


class TestClock(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(D.clock(0), "0:00")
        self.assertEqual(D.clock(61.4), "1:01")
        self.assertEqual(D.clock(3600), "1:00:00")
        self.assertEqual(D.clock(3725), "1:02:05")


class TestChapters(unittest.TestCase):
    CH = [{"t": 0.0, "title": "Intro"}, {"t": 12.0, "title": "Headshots"}, {"t": 40.0, "title": "Nades"}]

    def test_a_qualifying_list(self):
        r = D.chapter_lines(self.CH, 70.0)
        self.assertTrue(r["qualifies"])
        self.assertEqual(r["text"].splitlines()[1], "0:12 Headshots")
        self.assertEqual(r["problems"], [])

    def test_each_of_youtubes_conditions(self):
        self.assertFalse(D.chapter_lines([], 60)["qualifies"])
        late = D.chapter_lines([{"t": 5.0, "title": "a"}, {"t": 20.0, "title": "b"}, {"t": 40.0, "title": "c"}], 60)
        self.assertIn("0:00", " ".join(late["problems"]))
        few = D.chapter_lines(self.CH[:2], 60)
        self.assertIn("at least 3", " ".join(few["problems"]))
        short = D.chapter_lines([{"t": 0.0, "title": "a"}, {"t": 5.0, "title": "b"}, {"t": 40.0, "title": "c"}], 60)
        self.assertIn("10 s", " ".join(short["problems"]))

    def test_the_last_chapter_runs_to_the_end(self):
        r = D.chapter_lines(self.CH, 45.0)          # "Nades" would last 5 s
        self.assertFalse(r["qualifies"])

    def test_unsorted_input_is_sorted(self):
        r = D.chapter_lines(list(reversed(self.CH)), 70.0)
        self.assertEqual(r["text"].splitlines()[0], "0:00 Intro")


class TestThumbnailCandidates(unittest.TestCase):
    def _tl(self):
        return timeline([
            {"id": "a", "src": "v", "in": 10, "out": 20, "markers": [{"at": 15, "name": "kill"}]},
            {"id": "b", "src": "w", "in": 0, "out": 10},
            {"id": "c", "src": "w", "in": 30, "out": 40, "markers": [{"at": 30.1, "name": "kill"}]},
        ])

    def test_frames_come_just_after_a_marker(self):
        c = D.thumbnail_candidates(self._tl())
        self.assertAlmostEqual(c[0][0], 5.0 + D.MARKER_LAG, places=2)

    def test_a_clip_without_markers_offers_its_middle(self):
        times = {r: t for t, r in D.thumbnail_candidates(self._tl())}
        self.assertAlmostEqual(times["b middle"], 15.0, places=2)

    def test_nothing_within_the_seam_margin(self):
        for t, _ in D.thumbnail_candidates(self._tl()):
            for seam in (10.0, 20.0):
                self.assertGreaterEqual(abs(t - seam), D.SEAM_MARGIN)

    def test_a_marker_that_would_land_on_a_join_is_dropped(self):
        tl = timeline([
            {"id": "a", "src": "v", "in": 10, "out": 20},
            {"id": "b", "src": "w", "in": 0, "out": 10, "markers": [{"at": 9.3, "name": "kill"}, {"at": 9.9, "name": "kill"}]},
            {"id": "c", "src": "w", "in": 30, "out": 40}])
        # 9.3 + 0.45 lands 0.25 s before the next join; 9.9 + 0.45 is past the end of the clip
        self.assertFalse(any(r.startswith("b kill") for _, r in D.thumbnail_candidates(tl)))

    def test_cards_never_offer_frames(self):
        tl = timeline([{"id": "i", "type": "card", "dur": 3.0}, {"id": "a", "src": "v", "in": 10, "out": 20}])
        self.assertTrue(all(not r.startswith("i ") for _, r in D.thumbnail_candidates(tl)))


class TestScoring(unittest.TestCase):
    def test_sharp_beats_blurred_beats_flat(self):
        rng = np.random.RandomState(0)
        sharp = rng.rand(90, 160) * 255
        blurred = sharp.copy()
        for _ in range(6):
            blurred[1:-1, 1:-1] = (blurred[:-2, 1:-1] + blurred[2:, 1:-1] + blurred[1:-1, :-2] + blurred[1:-1, 2:]) / 4
        flat = np.full((90, 160), 128.0)
        self.assertGreater(D.sharpness(sharp), D.sharpness(blurred))
        self.assertGreater(D.sharpness(blurred), D.sharpness(flat))
        self.assertEqual(D.sharpness(flat), 0.0)

    def test_tiny_images(self):
        self.assertEqual(D.sharpness(np.zeros((2, 2))), 0.0)

    def test_exposure(self):
        self.assertEqual(D.exposure_factor(120), 1.0)
        self.assertLess(D.exposure_factor(5), 0.5)
        self.assertLess(D.exposure_factor(250), 0.5)

    def test_spread_picks_the_best_and_keeps_them_apart(self):
        scored = [(10.0, 9.0, "a"), (11.0, 8.0, "b"), (30.0, 5.0, "c"), (60.0, 1.0, "d")]
        picked = D.pick_spread(scored, 2)
        self.assertEqual([p[2] for p in picked], ["a", "c"])
        self.assertEqual(len(D.pick_spread(scored, 10)), 3)       # d too, b never: it is 1 s from a


class TestVariants(unittest.TestCase):
    def test_the_blur_fit_graph_targets_the_variants_canvas(self):
        for name, (w, h, _) in D.VARIANTS.items():
            graph, label = D.variant_filter((1920, 1080), name)
            self.assertIn("scale={0}:{1}".format(w, h), graph, name)
            self.assertTrue(graph.endswith("[{0}]".format(label)))

    def test_unknown_variant(self):
        with self.assertRaises(E.SpecError):
            D.variant_filter((1920, 1080), "cinema")

    def test_shorts_cap_is_three_minutes(self):
        self.assertEqual(D.VARIANTS["shorts"][2], 180.0)


class TestPostText(unittest.TestCase):
    def test_titles_use_only_what_the_edit_holds(self):
        t = D.title_options("Teck", "Wardogs", 12, 0, 40.0)
        self.assertEqual(t[0], "Teck - 12 kills in Wardogs")
        self.assertFalse(any("nade" in x for x in t))
        self.assertFalse(any(" m kill" in x for x in t))            # 40 m is not worth a headline

    def test_nades_and_long_shots_appear_when_they_exist(self):
        t = D.title_options("Teck", "Wardogs", 12, 1, 238.0)
        self.assertTrue(any("1 nade kill " in x or "1 nade kill in" in x for x in t))
        self.assertTrue(any("238 m kill" in x for x in t))

    def test_description_carries_the_chapters(self):
        ch = D.chapter_lines([{"t": 0.0, "title": "Intro"}, {"t": 12.0, "title": "A"}, {"t": 40.0, "title": "B"}], 70)
        text = D.description("Title", ch, ["12 kills"])
        self.assertIn("Chapters:", text)
        self.assertIn("0:12 A", text)
        bad = D.chapter_lines([{"t": 0.0, "title": "Intro"}], 70)
        self.assertIn("will not chapter", D.description("Title", bad, []))


def _tools():
    try:
        from clipbot.config import load_settings
        from clipbot.utils import resolve_tool
        s = load_settings()
        return resolve_tool(s.tool("ffmpeg"), ""), resolve_tool(s.tool("ffprobe"), "")
    except Exception:  # noqa: BLE001
        w = shutil.which("ffmpeg"), shutil.which("ffprobe")
        return w if all(w) else None


class TestPackageWithRealFfmpeg(unittest.TestCase):
    def setUp(self):
        self.tools = _tools()
        if not self.tools:
            self.skipTest("ffmpeg is not available")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.master = self.dir / "m.mp4"
        subprocess.run([self.tools[0], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc2=size=640x360:rate=30:duration=20", "-f", "lavfi",
                        "-i", "sine=frequency=330:sample_rate=48000:duration=20", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(self.master)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_package(self):
        tl = timeline([
            {"id": "a", "src": "v", "in": 10, "out": 20, "markers": [{"at": 15, "name": "kill"}], "tags": ["chapter"], "label": "A"},
            {"id": "b", "src": "w", "in": 0, "out": 10, "markers": [{"at": 5, "name": "kill"}], "tags": ["chapter"], "label": "B"}])
        out = self.dir / "deliver"
        man = D.build_package(self.tools[0], self.master, tl, out, T.chapters(tl), variants=["shorts", "square"], thumbs=2,
                              player="Teck", game="Wardogs", kills=2, nades=0, far=None, src_wh=(640, 360))
        kinds = [f["kind"] for f in man["files"]]
        self.assertEqual(kinds[0], "master")
        self.assertEqual(kinds.count("variant"), 2)
        self.assertEqual(kinds.count("thumbnail"), 2)
        for f in man["files"]:
            self.assertTrue(Path(f["file"]).is_file())
            self.assertEqual(len(f["sha1"]), 40)
        size = subprocess.run([self.tools[1], "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                               "-of", "csv=p=0", str(out / "shorts.mp4")], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        self.assertEqual(size, "1080,1920")
        for f in man["files"]:
            if f["kind"] == "thumbnail":
                self.assertLessEqual(f["bytes"], D.THUMB_MAX_BYTES)
        self.assertEqual(json.loads((out / "manifest.json").read_text(encoding="utf-8"))["duration"], tl["duration"])
        self.assertIn("Title candidates", (out / "post.md").read_text(encoding="utf-8"))
        self.assertIn("#shorts", json.loads((out / "post.json").read_text(encoding="utf-8"))["hashtags"])

    def test_a_variant_longer_than_its_cap_is_skipped_with_a_note(self):
        tl = timeline([{"id": "a", "src": "v", "in": 0, "out": 10}])
        tl["duration"] = 400.0                      # pretend: the shape allows 180 s
        man = D.build_package(self.tools[0], self.master, tl, self.dir / "d2", [], variants=["shorts"], thumbs=0,
                              src_wh=(640, 360))
        self.assertFalse(any(f["kind"] == "variant" for f in man["files"]))
        self.assertTrue(any("skipped" in w for w in man["warnings"]))
        self.assertNotIn("#shorts", json.loads((self.dir / "d2" / "post.json").read_text(encoding="utf-8"))["hashtags"])


if __name__ == "__main__":
    unittest.main()
