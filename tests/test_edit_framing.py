"""The webcam stays in frame through a real render.

A synthetic 1080p source with a cyan-bordered "webcam" box that sits top-right (a hair from the
edge, like the YouTube layout) and jumps to the left-middle (the Kick layout) at 4.0 s. The clip
zooms 1.2x across that jump. The rendered file's frames are read back with the same detector that
mapped the scene, and the box must be where the geometry says - and, with safe framing off, shown
NOT to be, so the test cannot pass by the zoom being too small to matter.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from clipbot import editframe, editqa, editscene, editspec as E
from clipbot.config import Settings, load_settings
from clipbot.stages import edit as stage
from clipbot.utils import resolve_tool


def _tools():
    try:
        s = load_settings()
        return resolve_tool(s.tool("ffmpeg"), ""), resolve_tool(s.tool("ffprobe"), "")
    except Exception:  # noqa: BLE001
        w = shutil.which("ffmpeg"), shutil.which("ffprobe")
        return w if all(w) else None


COLOR = "0x23A0BE"


class TestWebcamStaysInFrame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()
        if not cls.tools:
            raise unittest.SkipTest("ffmpeg is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.src = d / "src.mp4"
        vf = ("drawbox=x=1392:y=118:w=508:h=286:color={c}:t=6:enable='lt(t,4)',"
              "drawbox=x=20:y=400:w=508:h=286:color={c}:t=6:enable='gte(t,4)'").format(c=COLOR)
        # a calm, game-coloured background (moving noise and a grid): no cyan anywhere but the box
        vf = "noise=alls=22:allf=t,drawgrid=w=240:h=135:t=2:c=white@0.25," + vf
        subprocess.run([cls.tools[0], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x2a2e36:size=1920x1080:rate=30:duration=9", "-f", "lavfi", "-i", "sine=frequency=300:duration=9",
                        "-vf", vf, "-c:v", "libx264", "-g", "15", "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.src)],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def settings(self, name, **edit):
        return Settings({"work_root": str(Path(self.tmp.name) / name), "tools": {"ffmpeg": self.tools[0], "ffprobe": self.tools[1]},
                         "edit": dict({"segment_pad": 0.5}, **edit)}, Path("settings.json"))

    def spec(self, name):
        return {"name": name, "profile": "balanced",
                "output": {"preset": "youtube_1080p30", "width": 960, "height": 540, "fps": 30, "x264_preset": "ultrafast"},
                "sources": {"v": {"file": str(self.src)}},
                "clips": [{"id": "a", "src": "v", "in": 1.0, "out": 7.0,
                           "camera": [{"at": 1.5, "zoom": 1.0}, {"at": 3.0, "zoom": 1.2, "ease": "out2"},
                                      {"at": 5.5, "zoom": 1.2, "ease": "linear"}, {"at": 6.5, "zoom": 1.0}]}],
                "tracks": {"music": [], "sfx": []}}

    def render(self, name, **edit):
        settings = self.settings(name, **edit)
        report = stage.render_edit(self.spec(name), settings, master=False)
        tl = json.loads((stage.edit_dir(settings, name) / "render" / "{0}.final.timeline.json".format(name)).read_text(encoding="utf-8"))
        self.timeline = tl["timeline"]
        return report, tl["timeline"]["clips"][0]

    def found_vs_expected(self, report, entry, t):
        """(box found in the rendered frame at clip second t, box the geometry expects there)."""
        _, frame = next(editscene.read_frames(self.tools[0], report["output"], t, t + 0.2, 30.0, (960, 540)))
        found = editscene.find_cam_box(frame)
        box = editscene.box_at(entry["scene"], t)
        z, x, y = editframe.camera_at(entry["camera"], t)
        return found, editframe.box_in_output(box, z, x, y)

    def close(self, a, b, tol=0.015):
        return a is not None and all(abs(a[k] - b[k]) <= tol for k in "xywh")

    def test_the_scene_map_sees_the_jump_and_is_recorded_in_the_timeline(self):
        _, entry = self.render("scene")
        self.assertEqual(len(entry["scene"]), 2, entry["scene"])
        self.assertAlmostEqual(entry["scene"][0]["t1"], 3.0, delta=0.05)      # source 4.0 s = clip second 3.0
        self.assertGreater(entry["scene"][0]["cam"]["x"], 0.5)
        self.assertLess(entry["scene"][1]["cam"]["x"], 0.1)

    def test_with_safe_framing_the_box_is_whole_where_the_geometry_says_before_and_after_the_jump(self):
        report, entry = self.render("safe")
        for t in (2.0, 2.6, 4.0, 5.0):
            found, want = self.found_vs_expected(report, entry, t)
            self.assertTrue(self.close(found, want), "t={0}: found {1}, expected {2}".format(t, found, want))
            self.assertGreaterEqual(want["x"], 0.0)
            self.assertLessEqual(want["x"] + want["w"], 1.0)

    def test_without_it_the_same_zoom_cuts_the_box_off(self):
        """The control: the centred zoom of the same clip. If this passed, the test above would prove nothing."""
        report, entry = self.render("legacy", safe_framing=False)
        self.assertNotIn("scene", entry)
        scene = editscene.scan_segment(self.tools[0], self.src, 1.0, 7.0)
        z, x, y = editframe.camera_at(entry["camera"], 2.6)
        want = editframe.box_in_output(editscene.box_at(scene, 2.6 + 1.0), z, x, y)      # clip second 2.6 is source second 3.6
        _, frame = next(editscene.read_frames(self.tools[0], report["output"], 2.6, 2.8, 30.0, (960, 540)))
        found = editscene.find_cam_box(frame)
        self.assertFalse(self.close(found, want), "found {0}, the full box would be {1}".format(found, want))
        self.assertGreater(want["x"] + want["w"], 1.0)                      # the geometry says it runs off the frame

    def test_verify_looks_at_the_rendered_frames_and_passes_a_safely_framed_clip(self):
        report, entry = self.render("verify-ok")
        timeline = {"clips": [entry], "duration": report["duration"], "events": [], "fps": 30}
        rows = editqa.webcam_in_frame(self.tools[0], report["output"], timeline, per_clip=3)
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["checked"], 2)
        self.assertEqual(rows[0]["matched"], rows[0]["checked"], rows)

    def test_verify_fails_when_the_frames_do_not_match_where_the_webcam_should_be(self):
        """The check can fail: hand it the centred keys while the file was rendered with the safe ones."""
        report, entry = self.render("verify-bad")
        wrong = dict(entry, camera=[dict(k, x=0.5, y=0.5) for k in entry["camera"] if "at" in k])
        timeline = {"clips": [wrong], "duration": report["duration"], "events": [], "fps": 30}
        rows = editqa.webcam_in_frame(self.tools[0], report["output"], timeline, per_clip=3)
        self.assertEqual(len(rows), 1)
        self.assertLess(rows[0]["matched"], rows[0]["checked"], rows)

    def test_the_full_verify_reports_the_check(self):
        report, entry = self.render("verify-full")
        sp = E.normalize(self.spec("verify-full"))
        timeline = self.timeline
        result = editqa.verify(self.tools[0], self.tools[1], report["output"], timeline, sp["master"],
                               Path(self.tmp.name) / "vout", deep=False)
        row = next(c for c in result["checks"] if c["name"] == "webcam in frame")
        self.assertEqual(row["status"], "pass", row)

    def test_a_title_that_would_sit_on_the_webcam_is_moved_off_it_and_the_render_keeps_the_webcam(self):
        spec = self.spec("moved")
        spec["clips"][0].update({"in": 0.5, "out": 3.5, "camera": [],
                                 "overlays": [{"kind": "text", "at": 0.5, "dur": 2.0, "x": 0.5, "y": 0.10, "size": 0.05,
                                               "text": "A VERY LONG LOWER THIRD THAT RUNS RIGHT INTO THE FACECAM"}]})
        settings = self.settings("moved")
        report = stage.render_edit(spec, settings, master=False)
        tl = json.loads((stage.edit_dir(settings, "moved") / "render" / "moved.final.timeline.json").read_text(encoding="utf-8"))
        entry = tl["timeline"]["clips"][0]
        self.assertNotEqual((entry["overlays"][0]["x"], entry["overlays"][0]["y"]), (0.5, 0.10))
        self.assertTrue(any("moved the overlay" in w for w in report["warnings"]), report["warnings"])
        # and the rendered frame still has the webcam box whole, undisturbed by the title
        _, frame = next(editscene.read_frames(self.tools[0], report["output"], 1.5, 1.7, 30.0, (960, 540)))
        found = editscene.find_cam_box(frame)
        self.assertTrue(self.close(found, {"x": 0.725, "y": 0.1093, "w": 0.2646, "h": 0.263}, tol=0.02), found)

    def test_a_render_with_no_webcam_in_the_source_is_unchanged_by_framing(self):
        plain = Path(self.tmp.name) / "plain.mp4"
        subprocess.run([self.tools[0], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x2a2e36:size=1920x1080:rate=30:duration=4", "-f", "lavfi", "-i", "sine=duration=4",
                        "-vf", "noise=alls=22:allf=t,drawgrid=w=240:h=135:t=2:c=white@0.25",
                        "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(plain)], check=True)
        spec = self.spec("nocam")
        spec["sources"]["v"] = {"file": str(plain)}
        spec["clips"][0].update({"in": 0.5, "out": 3.5, "camera": [{"at": 0.5, "zoom": 1.0}, {"at": 2.0, "zoom": 1.1}]})
        settings = self.settings("nocam")
        stage.render_edit(spec, settings, master=False)
        tl = json.loads((stage.edit_dir(settings, "nocam") / "render" / "nocam.final.timeline.json").read_text(encoding="utf-8"))
        entry = tl["timeline"]["clips"][0]
        self.assertEqual([s["cam"] for s in entry["scene"]], [None])
        self.assertTrue(all(k["x"] == 0.5 and k["y"] == 0.5 for k in entry["camera"]))


class TestCameraPivot(unittest.TestCase):
    """Regression: every camera move once pivoted on the top-left corner whatever its x/y, because crop
    read the scaled frame's size back as `iw` - which stays the first (unzoomed) frame's with eval=frame."""

    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()
        if not cls.tools:
            raise unittest.SkipTest("ffmpeg is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = Path(cls.tmp.name) / "mid.mp4"
        vf = "noise=alls=22:allf=t,drawgrid=w=240:h=135:t=2:c=white@0.25,drawbox=x=576:y=324:w=518:h=292:color={c}:t=6".format(c=COLOR)
        subprocess.run([cls.tools[0], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x2a2e36:size=1920x1080:rate=30:duration=6", "-vf", vf, "-c:v", "libx264", "-g", "15",
                        "-crf", "14", "-pix_fmt", "yuv420p", str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def peak_box(self, name, pan):
        settings = Settings({"work_root": str(Path(self.tmp.name) / name), "tools": {"ffmpeg": self.tools[0], "ffprobe": self.tools[1]},
                             "edit": {"segment_pad": 0.5, "safe_framing": False}}, Path("settings.json"))
        spec = {"name": name, "profile": "balanced", "output": {"preset": "youtube_1080p30", "width": 960, "height": 540, "fps": 30,
                                                                 "x264_preset": "ultrafast"},
                "sources": {"v": {"file": str(self.src)}}, "tracks": {"music": [], "sfx": []},
                "clips": [{"id": "a", "src": "v", "in": 0.5, "out": 5.0, "audio": {"mute": True},
                           "camera": [{"at": 0.5, "zoom": 1.0}, {"at": 2.0, "zoom": 1.4, "x": pan[0], "y": pan[1], "ease": "linear"},
                                      {"at": 4.0, "zoom": 1.4, "x": pan[0], "y": pan[1], "ease": "linear"}]}]}
        report = stage.render_edit(spec, settings, master=False)
        _, frame = next(editscene.read_frames(self.tools[0], report["output"], 2.5, 2.7, 30.0, (960, 540)))
        return editscene.find_cam_box(frame)

    def test_a_centred_zoom_that_starts_at_one_stays_centred(self):
        got = self.peak_box("pivot-c", (0.5, 0.5))
        want = editframe.box_in_output({"x": 0.3, "y": 0.3, "w": 0.2698, "h": 0.2704}, 1.4, 0.5, 0.5)
        self.assertIsNotNone(got)
        for k in "xywh":
            self.assertAlmostEqual(got[k], want[k], delta=0.015, msg=k)

    def test_the_pan_point_is_where_the_zoom_goes(self):
        got = self.peak_box("pivot-p", (0.7, 0.35))
        want = editframe.box_in_output({"x": 0.3, "y": 0.3, "w": 0.2698, "h": 0.2704}, 1.4, 0.7, 0.35)
        self.assertIsNotNone(got)
        for k in "xywh":
            self.assertAlmostEqual(got[k], want[k], delta=0.015, msg=k)


if __name__ == "__main__":
    unittest.main()
