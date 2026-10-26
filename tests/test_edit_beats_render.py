"""Beats and transitions through the real renderer (real ffmpeg on a tiny synthetic source; skipped without ffmpeg):
the freeze really holds the picture, a replay really adds its length, and a whip really blurs the join."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clipbot import editbeats as B
from clipbot import editscene, edittemplates as TPL, edittimeline as T
from clipbot import editspec as E
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


def sharpness(frame):
    """The variance of a Laplacian: how much fine detail is in the picture (a blur drives it down)."""
    g = frame.astype(float).mean(axis=2)
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


class TestBeatsRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()
        if not cls.tools:
            raise unittest.SkipTest("ffmpeg is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.src = d / "src.mp4"
        subprocess.run([cls.tools[0], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=20",
                        "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=20", "-c:v", "libx264", "-g", "15",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.src)], check=True)
        cls.settings = Settings({"work_root": str(d / "work"), "tools": {"ffmpeg": cls.tools[0], "ffprobe": cls.tools[1]},
                                 "edit": {"segment_pad": 0.5, "safe_framing": False}}, Path("settings.json"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def spec(self, name, clips, sfx=()):
        return {"name": name, "profile": "balanced", "output": {"preset": "youtube_1080p30", "width": 320, "height": 180, "fps": 30, "x264_preset": "ultrafast"},
                "sources": {"v": {"file": str(self.src)}}, "clips": clips, "tracks": {"music": [], "sfx": list(sfx)}}

    def render(self, spec):
        report = stage.render_edit(spec, self.settings)
        tl = T.compile_timeline(E.normalize(spec))
        return report, tl

    def frame(self, path, t):
        _, f = next(editscene.read_frames(self.tools[0], path, t, t + 0.1, 30.0, (320, 180)))
        return f

    def test_a_freeze_zoom_holds_the_picture_and_adds_exactly_its_hold(self):
        ctx = B.Ctx("a", "v", 2.0, 10.0)
        frag = B.expand({"do": "freeze_zoom", "at": 6.0, "hold": 0.9, "flash": 0}, ctx)
        cam = TPL._ambient_camera(2.0, 10.0, E.PROFILES["balanced"], [(p["at"], p["zoom"] - 1.0) for p in frag["punches"]])
        clip = {"id": "a", "src": "v", "in": 2.0, "out": 10.0, "freezes": frag["freezes"], "camera": cam, "markers": frag["markers"]}
        report, tl = self.render(self.spec("fz", [clip]))
        plain = T.compile_timeline(E.normalize(self.spec("fzp", [{"id": "a", "src": "v", "in": 2.0, "out": 10.0}])))["duration"]
        self.assertAlmostEqual(report["duration"] - plain, 0.9, delta=0.05)
        e = tl["clips"][0]
        t0 = e["start"] + edittimeline_out(e, 6.0)
        # inside the hold the picture does not move (encoding noise only: 0.2-0.5 grey levels measured); outside it does (27-37)
        diff = lambda a, b: float(np.abs(self.frame(report["output"], a).astype(int) - self.frame(report["output"], b).astype(int)).mean())
        self.assertLess(diff(t0 + 0.25, t0 + 0.6), 2.0, "the frame changed during the freeze")
        self.assertGreater(diff(t0 - 0.4, t0 - 0.2), 10.0)                   # the control: before the freeze the picture moves
        self.assertGreater(diff(t0 + 0.6, t0 + 1.4), 10.0)                   # and after it does again

    def test_the_punch_makes_the_held_frame_bigger_than_the_frame_before_it(self):
        ctx = B.Ctx("a", "v", 2.0, 10.0)
        frag = B.expand({"do": "freeze_zoom", "at": 6.0, "hold": 0.9, "flash": 0, "zoom": 1.5}, ctx)
        cam = TPL._ambient_camera(2.0, 10.0, E.PROFILES["balanced"], [(p["at"], p["zoom"] - 1.0) for p in frag["punches"]])
        zoomed = {"id": "a", "src": "v", "in": 2.0, "out": 10.0, "freezes": frag["freezes"], "camera": cam}
        plain_freeze = {"id": "a", "src": "v", "in": 2.0, "out": 10.0, "freezes": frag["freezes"]}
        r1, tl = self.render(self.spec("fzz", [zoomed]))
        r2, _ = self.render(self.spec("fzn", [plain_freeze]))
        e = tl["clips"][0]
        t = e["start"] + edittimeline_out(e, 6.0) + 0.5
        big, flat = self.frame(r1["output"], t), self.frame(r2["output"], t)
        self.assertFalse(np.array_equal(big, flat))
        self.assertLess(sharpness(big), sharpness(flat))                     # scaled up 1.5x from 320x180: softer, fewer fine lines

    def test_a_replay_adds_its_range_over_its_rate_and_a_whip_blurs_the_join(self):
        host = {"id": "a", "src": "v", "in": 2.0, "out": 8.0}
        rep = B.expand({"do": "replay", "at": 6.0, "back": 2.0, "forward": 1.0, "rate": 0.5}, B.Ctx("a", "v", 2.0, 8.0))["after"][0]
        nxt = {"id": "b", "src": "v", "in": 11.0, "out": 17.0}
        B.apply_transition("whip", host, nxt, {})
        report, tl = self.render(self.spec("rw", [host, rep, nxt]))
        ids = [c["id"] for c in tl["clips"]]
        self.assertEqual(ids, ["a", "a" + "r1", "b"])
        self.assertAlmostEqual(tl["clips"][1]["dur"], 6.0, delta=0.07)
        self.assertTrue(report["xfade"])
        join = tl["clips"][2]["start"]                                       # the whip's overlap begins where the incoming clip does
        mid = self.frame(report["output"], join + tl["clips"][2]["overlap_in"] / 2.0)
        around = (self.frame(report["output"], join - 0.5), self.frame(report["output"], join + 1.0))
        self.assertLess(sharpness(mid), 0.7 * min(sharpness(f) for f in around), "the whip did not blur the join")


def edittimeline_out(entry, src_t):
    return T.src_to_out(entry["pieces"], src_t)


if __name__ == "__main__":
    unittest.main()
