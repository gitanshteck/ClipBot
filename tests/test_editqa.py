"""Tests for clipbot/editqa.py: the checks run on a finished render.

The detectors are tested against media with a known flaw built on the spot - a black
stretch, a silent stretch, a frozen stretch, a phase-jump click - because "it found
nothing" proves nothing about a detector until it has been shown one it must find.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clipbot import editaudio, editqa as Q, editspec as E, edittimeline as T


def _tools():
    try:
        from clipbot.config import load_settings
        from clipbot.utils import resolve_tool
        s = load_settings()
        return resolve_tool(s.tool("ffmpeg"), ""), resolve_tool(s.tool("ffprobe"), "")
    except Exception:  # noqa: BLE001
        w = shutil.which("ffmpeg"), shutil.which("ffprobe")
        return w if all(w) else None


class TestPure(unittest.TestCase):
    def test_markdown_lists_every_check_and_artefact(self):
        result = {"ok": False, "file": "x.mp4", "checks": [
            {"name": "loudness", "status": "pass", "detail": "-14.0 LUFS"},
            {"name": "clicks at joins", "status": "warn", "detail": "1 join pops"},
            {"name": "A/V length", "status": "fail", "detail": "40 ms out"}],
            "artifacts": {"waveform": "w.png", "storyboard": ["s1.jpg"]}}
        md = Q.render_markdown(result)
        self.assertIn("FAILED", md)
        self.assertIn("[ok  ] loudness", md)
        self.assertIn("[WARN] clicks at joins", md)
        self.assertIn("[FAIL] A/V length", md)
        self.assertIn("w.png", md)
        self.assertIn("s1.jpg", md)

    def test_pacing_checks_fail_a_raw_clip_and_pass_a_moving_one(self):
        raw = T.compile_timeline(E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}},
                                              "clips": [{"id": "a", "src": "v", "in": 1, "out": 12}]}))
        names = {c["name"]: c["status"] for c in Q._pacing_checks(raw)}
        self.assertEqual(names["pacing"], "warn")
        moving = T.compile_timeline(E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [
            {"id": "a", "src": "v", "in": 1, "out": 12,
             "camera": [{"at": 1 + 2 * i, "zoom": 1.0 + 0.02 * (i % 2)} for i in range(6)],
             "overlays": [{"kind": "text", "at": 1, "dur": 2, "text": "X"}]}]}))
        self.assertEqual({c["name"]: c["status"] for c in Q._pacing_checks(moving)}["pacing"], "pass")

    def test_waveform_png_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            sr = editaudio.ANALYSIS_RATE
            x = (0.3 * np.sin(2 * np.pi * 220 * np.arange(sr * 3) / sr)).astype(np.float32)
            out = Q.waveform_png(x, sr, Path(d) / "w.png", markers=[(1.0, "cut")], target_lufs=-14.0)
            from PIL import Image
            with Image.open(out) as im:
                self.assertEqual(im.size, (1800, 360))


@unittest.skipUnless(_tools(), "ffmpeg is not available")
class TestDetectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ff, cls.fp = _tools()
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.d = d

        def make(name, vf, af, dur):
            out = d / name
            subprocess.run([cls.ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=30:duration={0}".format(dur), "-f", "lavfi", "-i",
                            "sine=frequency=300:sample_rate=48000:duration={0}".format(dur), "-vf", vf, "-af", af,
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)], check=True)
            return out

        cls.clean = make("clean.mp4", "null", "volume=0.5", 8)
        cls.black = make("black.mp4", "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,2,4)'", "volume=0.5", 8)
        cls.silent = make("silent.mp4", "null", "volume=0.5,volume=enable='between(t,2,5)':volume=0", 8)
        cls.frozen = make("frozen.mp4", "loop=loop=90:size=1:start=60", "volume=0.5", 8)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_black_is_found_where_it_is(self):
        found = Q.black_frames(self.ff, self.black)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0]["start"], 2.0, delta=0.1)
        self.assertAlmostEqual(found[0]["dur"], 2.0, delta=0.15)
        self.assertEqual(Q.black_frames(self.ff, self.clean), [])

    def test_silence_is_found_where_it_is(self):
        found = Q.silences(self.ff, self.silent)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0]["start"], 2.0, delta=0.15)
        self.assertAlmostEqual(found[0]["dur"], 3.0, delta=0.3)
        self.assertEqual(Q.silences(self.ff, self.clean), [])

    def test_a_frozen_stretch_is_found(self):
        found = Q.frozen_frames(self.ff, self.frozen)
        self.assertTrue(found and found[0]["dur"] > 2.0, found)
        self.assertEqual(Q.frozen_frames(self.ff, self.clean), [])

    def test_loudness_is_measured(self):
        m = Q.loudness(self.ff, self.clean)
        self.assertIsNotNone(m)
        self.assertTrue(-40 < m["lufs"] < -10)

    def test_probe_reports_the_streams(self):
        info = Q.probe(self.fp, self.clean)
        self.assertEqual((info["video"]["width"], info["video"]["height"]), (320, 180))
        self.assertEqual(info["audio"]["sample_rate"], "48000")

    def test_storyboard_makes_labelled_sheets(self):
        sheets = Q.storyboard(self.ff, self.clean, [(0.5, "a"), (3.0, "b"), (6.0, "c")], self.d / "board", cols=2, rows=2, thumb_w=160)
        self.assertEqual(len(sheets), 1)
        from PIL import Image
        with Image.open(sheets[0]) as im:
            self.assertEqual(im.size, (320, 180))

    def test_verify_passes_a_clean_file_and_flags_a_black_one(self):
        ok = Q.verify(self.ff, self.fp, self.clean, None, None, self.d / "v1", deep=True)
        self.assertTrue(ok["ok"])
        self.assertEqual([c for c in ok["checks"] if c["name"] == "black frames"][0]["status"], "pass")
        bad = Q.verify(self.ff, self.fp, self.black, None, None, self.d / "v2", deep=True)
        self.assertEqual([c for c in bad["checks"] if c["name"] == "black frames"][0]["status"], "warn")

    def test_a_planned_title_card_is_not_reported_as_black(self):
        tl = {"duration": 8.0, "fps": 30.0, "profile": "punchy", "tracks": {"music": [], "sfx": []}, "events": [], "warnings": [],
              "clips": [{"id": "i", "index": 0, "type": "card", "start": 2.0, "end": 4.0, "dur": 2.0, "overlap_in": 0.0,
                         "transition_in": {"type": "cut", "dur": 0.0}, "markers": [], "overlays": [], "camera": [], "fx": [],
                         "tags": [], "label": "i", "pieces": [], "plain": True}]}
        res = Q.verify(self.ff, self.fp, self.black, tl, None, self.d / "v3", deep=True)
        self.assertEqual([c for c in res["checks"] if c["name"] == "black frames"][0]["status"], "pass")

    def test_a_contact_sheet_is_made_of_every_marker_moment(self):
        spec = E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "output": {"fps": 30},
                            "clips": [{"id": "a", "src": "v", "in": 0, "out": 8,
                                       "markers": [{"at": 2, "name": "kill"}, {"at": 5, "name": "kill"}]}]})
        res = Q.verify(self.ff, self.fp, self.clean, T.compile_timeline(spec), spec["master"], self.d / "v5", deep=False)
        self.assertEqual(len(res["artifacts"]["moments"]), 1)
        self.assertTrue(Path(res["artifacts"]["moments"][0]).is_file())
        self.assertIn("marker moments", Q.render_markdown(res))

    def test_a_length_mismatch_with_the_plan_fails(self):
        tl = {"duration": 12.0, "fps": 30.0, "profile": "punchy", "tracks": {"music": [], "sfx": []}, "events": [], "warnings": [],
              "clips": []}
        res = Q.verify(self.ff, self.fp, self.clean, tl, None, self.d / "v4", deep=False)
        self.assertFalse(res["ok"])
        self.assertEqual([c for c in res["checks"] if c["name"] == "length vs plan"][0]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
