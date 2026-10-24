"""Captions through a real render: the .ass is written, libass burns it where the box is, a clip without captions is
untouched, and the clip cache notices a change of text. Skipped without ffmpeg."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clipbot import editqa, editscene
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


W, H = 960, 540
# the box the caption is drawn in, as fractions of the frame (x 0.16-0.68, bottom at 0.915, a line about 0.06 tall)
BOX = (int(0.16 * W), int(0.83 * H), int(0.68 * W), int(0.93 * H))


class TestCaptionsRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()
        if not cls.tools:
            raise unittest.SkipTest("ffmpeg is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = Path(cls.tmp.name) / "src.mp4"
        subprocess.run([cls.tools[0], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x2a2e36:size=1920x1080:rate=30:duration=9",
                        "-f", "lavfi", "-i", "sine=frequency=300:duration=9", "-vf", "noise=alls=14:allf=t", "-c:v", "libx264", "-g", "15", "-crf", "14",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def settings(self, name):
        return Settings({"work_root": str(Path(self.tmp.name) / name), "tools": {"ffmpeg": self.tools[0], "ffprobe": self.tools[1]},
                         "edit": {"segment_pad": 0.5, "safe_framing": False}}, Path("settings.json"))

    def spec(self, name, block=True, **clip):
        d = {"name": name, "profile": "balanced",
             "output": {"preset": "youtube_1080p30", "width": W, "height": H, "fps": 30, "x264_preset": "ultrafast"},
             "sources": {"v": {"file": str(self.src)}},
             "clips": [dict({"id": "a", "src": "v", "in": 1.0, "out": 7.0}, **clip)],
             "tracks": {"music": [], "sfx": []}}
        if block:
            d["captions"] = {"highlight": "#35D0FF"}
        return d

    def write_captions(self, settings, name, text="hum pe mortar chalu ho gaya", s=3.0, e=5.5):
        words = [{"t": w, "s": s + i * 0.4, "e": s + i * 0.4 + 0.35} for i, w in enumerate(text.split())]
        doc = {"version": 1, "model": None, "clips": {"a": {"cues": [{"s": s, "e": e, "text": text, "words": words}]}}}
        root = stage.edit_dir(settings, name)
        root.mkdir(parents=True, exist_ok=True)
        (root / "captions.json").write_text(json.dumps(doc), encoding="utf-8")

    def render(self, name, spec=None, text="hum pe mortar chalu ho gaya"):
        settings = self.settings(name)
        self.write_captions(settings, name, text)
        report = stage.render_edit(spec or self.spec(name), settings, master=False)
        return settings, report

    def frame(self, report, t):
        _, f = next(editscene.read_frames(self.tools[0], report["output"], t, t + 0.2, 30.0, (W, H)))
        return f

    @staticmethod
    def text_pixels(frame):
        x0, y0, x1, y1 = BOX
        box = frame[y0:y1, x0:x1].astype(int)
        white = (box.min(axis=2) > 225)
        cyan = (box[..., 2] > 200) & (box[..., 1] > 150) & (box[..., 0] < 120)       # the highlight, in RGB
        return int(white.sum() + cyan.sum())

    def test_the_caption_is_burned_in_while_its_cue_is_up_and_not_before_or_after(self):
        _, report = self.render("on")
        during = self.text_pixels(self.frame(report, 3.0 - 1.0 + 1.5))          # clip second 3.5: inside 2.0-4.5
        before = self.text_pixels(self.frame(report, 0.4))
        after = self.text_pixels(self.frame(report, 5.0))
        self.assertGreater(during, 400)
        self.assertLess(before, 30)
        self.assertLess(after, 30)

    def test_the_highlight_colour_appears_as_the_words_are_spoken(self):
        _, report = self.render("hl")
        late = self.frame(report, 4.2)                                           # clip second 4.2: most words said
        x0, y0, x1, y1 = BOX
        box = late[y0:y1, x0:x1].astype(int)
        cyan = int(((box[..., 2] > 200) & (box[..., 1] > 150) & (box[..., 0] < 120)).sum())
        self.assertGreater(cyan, 100)

    def test_no_captions_block_in_the_spec_draws_nothing_even_if_captions_json_exists(self):
        _, report = self.render("noblock", spec=self.spec("noblock", block=False))
        self.assertLess(self.text_pixels(self.frame(report, 3.5)), 30)

    def test_a_clip_marked_captions_false_has_none(self):
        _, report = self.render("off", spec=self.spec("off", captions=False))
        self.assertLess(self.text_pixels(self.frame(report, 3.5)), 30)

    def test_a_clip_with_a_kill_callout_has_no_captions_unless_asked(self):
        kill = {"markers": [{"at": 4.0, "name": "kill"}]}
        _, report = self.render("kill", spec=self.spec("kill", **kill))
        self.assertLess(self.text_pixels(self.frame(report, 3.5)), 30)
        _, forced = self.render("kill2", spec=self.spec("kill2", **dict(kill, captions=True)))
        self.assertGreater(self.text_pixels(self.frame(forced, 3.5)), 400)

    def test_captions_are_on_the_timeline_in_output_seconds_for_verify(self):
        settings, report = self.render("tl")
        tl = json.loads((stage.edit_dir(settings, "tl") / "render" / "tl.final.timeline.json").read_text(encoding="utf-8"))
        entry = tl["timeline"]["clips"][0]
        cue = entry["captions"]["cues"][0]
        self.assertAlmostEqual(cue["s"], 2.0, places=2)                         # source 3.0 s, clip in at 1.0 s
        self.assertAlmostEqual(cue["e"], 4.5, places=2)

    def test_changing_the_text_re_renders_the_clip_and_leaving_it_reuses_it(self):
        settings = self.settings("cache")
        self.write_captions(settings, "cache")
        first = stage.render_edit(self.spec("cache"), settings, master=False)
        clips = sorted((stage.edit_dir(settings, "cache") / "cache" / "final" / "clips").glob("a-*.mp4"))
        again = stage.render_edit(self.spec("cache"), settings, master=False)
        self.assertEqual(sorted((stage.edit_dir(settings, "cache") / "cache" / "final" / "clips").glob("a-*.mp4")), clips)
        self.write_captions(settings, "cache", text="ek aur line yahan")
        stage.render_edit(self.spec("cache"), settings, master=False)
        self.assertEqual(len(list((stage.edit_dir(settings, "cache") / "cache" / "final" / "clips").glob("a-*.mp4"))), len(clips) + 1)

    def test_a_missing_captions_json_is_a_warning_that_says_what_to_run_not_an_error(self):
        settings = self.settings("missing")
        report = stage.render_edit(self.spec("missing"), settings, master=False)
        self.assertTrue(any("edit captions" in w for w in report["warnings"]), report["warnings"])


    def timeline(self, settings, name):
        return json.loads((stage.edit_dir(settings, name) / "render" / "{0}.final.timeline.json".format(name)).read_text(encoding="utf-8"))["timeline"]

    def test_verify_finds_the_caption_text_in_a_captioned_render(self):
        settings, report = self.render("qa-on")
        rows = editqa.captions_on_screen(self.tools[0], report["output"], self.timeline(settings, "qa-on"))
        self.assertEqual([(r["clip"], r["drawn"]) for r in rows], [("a", 1)])
        self.assertFalse(rows[0]["small"])

    def test_verify_fails_when_the_timeline_says_captions_but_the_render_has_none(self):
        """The check can fail: a render made without the block, judged against a timeline that claims captions."""
        settings, report = self.render("qa-off", spec=self.spec("qa-off", block=False))
        tl = self.timeline(settings, "qa-off")
        tl["clips"][0]["captions"] = {"cues": [{"s": 2.0, "e": 4.5, "text": "x", "words": []}], "style": {}}
        rows = editqa.captions_on_screen(self.tools[0], report["output"], tl)
        self.assertEqual(rows[0]["drawn"], 0)

    def test_a_small_caption_size_is_flagged(self):
        settings, report = self.render("qa-small")
        tl = self.timeline(settings, "qa-small")
        tl["clips"][0]["captions"]["style"]["size"] = 0.03
        self.assertTrue(editqa.captions_on_screen(self.tools[0], report["output"], tl)[0]["small"])


if __name__ == "__main__":
    unittest.main()
