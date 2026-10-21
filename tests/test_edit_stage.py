"""Tests for clipbot/stages/edit.py.

The pure helpers are tested directly. The renderer itself is tested end to end with
the real ffmpeg on tiny synthetic media - a stubbed ffmpeg would only prove the
control flow, and the thing worth proving here is the arithmetic: that picture and
sound come out the same length as the timeline said, that a re-render reuses its
clips, that mastering lands near the loudness target. Skipped when ffmpeg is absent.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipbot import editqa, editspec as E, edittimeline as T
from clipbot.config import Settings, load_settings
from clipbot.specerror import SpecError
from clipbot.stages import edit as stage
from clipbot.utils import StageError, resolve_tool


def _tools():
    try:
        s = load_settings()
        return resolve_tool(s.tool("ffmpeg"), ""), resolve_tool(s.tool("ffprobe"), "")
    except Exception:  # noqa: BLE001
        w = shutil.which("ffmpeg"), shutil.which("ffprobe")
        return w if all(w) else None


def _settings(tmp, tools):
    return Settings({"work_root": str(tmp), "tools": {"ffmpeg": tools[0], "ffprobe": tools[1]},
                     "edit": {"segment_pad": 0.5}}, Path("settings.json"))


class TestPureHelpers(unittest.TestCase):
    def test_paths_live_under_underscore_folders(self):
        s = Settings({"work_root": "D:/w"}, Path("s.json"))
        self.assertEqual(stage.edit_dir(s, "reel").parts[-2:], ("_edits", "reel"))
        self.assertEqual(stage.cache_root(s).parts[-2:], ("_cache", "edit"))

    def test_preview_output_is_a_540p_proxy_of_the_same_shape(self):
        out = E.OUTPUT_PRESETS["youtube_1080p60"]
        p = stage.preview_output(out)
        self.assertEqual((p["width"], p["height"]), (960, 540))
        self.assertEqual(p["fps"], 30)
        self.assertTrue(p["preview"])
        v = stage.preview_output(E.OUTPUT_PRESETS["shorts_1080x1920"])
        self.assertEqual((v["width"], v["height"]), (540, 960))

    def test_preview_is_never_larger_than_the_final(self):
        small = dict(E.OUTPUT_PRESETS["youtube_1080p30"], width=320, height=180)
        p = stage.preview_output(small)
        self.assertEqual((p["width"], p["height"]), (320, 180))

    def test_preview_never_raises_the_frame_rate(self):
        out = dict(E.OUTPUT_PRESETS["youtube_1080p30"], fps=24)
        self.assertEqual(stage.preview_output(out)["fps"], 24)

    def test_encode_tiers(self):
        out = E.OUTPUT_PRESETS["youtube_1080p60"]
        final = stage._video_encode_args(out, "final")
        self.assertIn("slow", final)
        self.assertEqual(final[final.index("-g") + 1], "30")           # 0.5 s at 60 fps
        self.assertEqual(final[final.index("-bf") + 1], "2")
        self.assertIn("open-gop=0:scenecut=0", final)
        self.assertIn("ultrafast", stage._video_encode_args(out, "preview"))
        mid = stage._video_encode_args(out, "intermediate")
        self.assertEqual(mid[mid.index("-crf") + 1], "14")

    def test_fingerprint_tracks_what_shapes_the_clip(self):
        sp = E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [{"id": "a", "src": "v", "in": 1, "out": 5}]})
        tl = T.compile_timeline(sp)

        def fp(clip=sp["clips"][0], tier="final", seg="k"):
            return stage._clip_fingerprint(clip, tl["clips"][0], sp["output"], tier, "tag", seg, None, None, [], "")

        self.assertEqual(fp(), fp())
        self.assertNotEqual(fp(), fp(tier="preview"))
        self.assertNotEqual(fp(), fp(seg="other"))
        self.assertNotEqual(fp(), fp(clip=dict(sp["clips"][0], **{"in": 2.0})))

    def test_load_spec(self):
        with self.assertRaises(SpecError):
            stage.load_spec("no-such-file.json")
        sp = stage.load_spec({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [{"id": "a", "src": "v", "in": 1, "out": 5}]})
        self.assertEqual(sp["name"], "t")


class TestRenderEdit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()
        if not cls.tools:
            raise unittest.SkipTest("ffmpeg is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.src = d / "src.mp4"
        cls.sfx = d / "hit.wav"
        cls.music = d / "bed.wav"
        run = lambda *a: subprocess.run([cls.tools[0], "-hide_banner", "-loglevel", "error", "-y"] + list(a), check=True)
        run("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=10", "-f", "lavfi",
            "-i", "sine=frequency=300:sample_rate=48000:duration=10", "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(cls.src))
        run("-f", "lavfi", "-i", "sine=frequency=1200:sample_rate=48000:duration=0.3", str(cls.sfx))
        run("-f", "lavfi", "-i", "sine=frequency=110:sample_rate=48000:duration=12", str(cls.music))
        cls.settings = _settings(d / "work", cls.tools)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def spec(self, name="e1", **extra):
        d = {
            "name": name, "profile": "balanced",
            "output": {"preset": "youtube_1080p30", "width": 320, "height": 180, "fps": 30, "x264_preset": "ultrafast"},
            "sources": {"v": {"file": str(self.src)}},
            "clips": [
                {"id": "i", "type": "card", "dur": 1.0, "bg": "#101010",
                 "overlays": [{"kind": "text", "at": 0.0, "dur": 1.0, "text": "TEST", "x": 0.5, "y": 0.5, "size": 0.1}]},
                {"id": "a", "src": "v", "in": 2, "out": 6, "markers": [{"at": 4, "name": "kill"}],
                 "speed": [{"at": 3, "rate": 1.0}, {"at": 3.5, "rate": 0.5}, {"at": 4.5, "rate": 0.5}, {"at": 5, "rate": 1.0}],
                 "camera": [{"at": 3, "zoom": 1.0}, {"at": 4, "zoom": 1.15, "ease": "out2"}],
                 "overlays": [{"kind": "callout", "at": 4, "dur": 1.5, "text": "kill", "sub": "12 m"}]},
                {"id": "b", "src": "v", "in": 1, "out": 4, "transition_in": {"type": "xfade", "name": "fade", "dur": 0.4}},
            ],
            "tracks": {"music": [{"id": "bed", "file": str(self.music), "gain_db": -20}],
                       "sfx": [{"id": "s1", "file": str(self.sfx), "on": {"clip": "a", "at": 4}}]},
        }
        d.update(extra)
        return d

    def probe_duration(self, path, stream):
        out = subprocess.run([self.tools[1], "-v", "error", "-select_streams", stream, "-show_entries", "stream=duration",
                              "-of", "csv=p=0", str(path)], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        return float(out)

    def test_a_full_render_is_the_planned_length_in_sync_and_on_target(self):
        report = stage.render_edit(self.spec("full"), self.settings)
        out = Path(report["output"])
        self.assertTrue(out.is_file())
        planned = T.compile_timeline(E.normalize(self.spec("full")))["duration"]
        self.assertAlmostEqual(report["duration"], planned, places=2)
        self.assertAlmostEqual(self.probe_duration(out, "v:0"), planned, delta=1.0 / 30 + 0.01)
        self.assertAlmostEqual(self.probe_duration(out, "a:0"), planned, delta=0.05)
        self.assertTrue(report["xfade"])
        self.assertEqual(report["loudness_target"], -14.0)
        spec = E.normalize(self.spec("full"))
        result = editqa.verify(self.tools[0], self.tools[1], out, T.compile_timeline(spec), spec["master"],
                               Path(self.tmp.name) / "verify", deep=False)
        self.assertTrue(result["ok"], result["checks"])
        loud = next(c for c in result["checks"] if c["name"] == "loudness")
        self.assertIn(loud["status"], ("pass", "warn"))
        lufs = float(loud["detail"].split()[0])
        self.assertAlmostEqual(lufs, -14.0, delta=2.0)

    def test_the_spec_on_disk_is_never_replaced_by_a_render(self):
        stage.render_edit(self.spec("rec"), self.settings, preview=True)
        root = stage.edit_dir(self.settings, "rec")
        self.assertFalse((root / "spec.json").exists())
        rec = json.loads((root / "render" / "rec.preview.spec.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["output"]["width"], 320)      # already smaller than the 540p proxy: kept
        self.assertTrue(rec["output"]["preview"])

    def test_a_second_render_reuses_every_clip(self):
        stage.render_edit(self.spec("cache"), self.settings, preview=True)
        labels = []
        real = stage._run_ffmpeg

        def spy(argv, *a, **k):
            labels.append(k.get("label"))
            return real(argv, *a, **k)

        with mock.patch.object(stage, "_run_ffmpeg", side_effect=spy):
            stage.render_edit(self.spec("cache"), self.settings, preview=True)
        self.assertFalse({"i", "a", "b"} & set(labels), "a clip was re-encoded: {0}".format(labels))
        self.assertFalse([l for l in labels if l and l.endswith("segment")], "a source segment was re-cut")

    def test_changing_one_clip_re_encodes_only_that_clip(self):
        stage.render_edit(self.spec("one"), self.settings, preview=True)
        changed = self.spec("one")
        changed["clips"][2]["out"] = 3.5
        calls = []
        real = stage._run_ffmpeg

        def spy(argv, *a, **k):
            calls.append(k.get("label") if "label" in k else None)
            return real(argv, *a, **k)

        with mock.patch.object(stage, "_run_ffmpeg", side_effect=spy):
            stage.render_edit(changed, self.settings, preview=True)
        self.assertIn("b", calls)
        self.assertNotIn("a", calls)

    def test_a_cut_only_edit_assembles_by_copy(self):
        sp = self.spec("cuts")
        sp["clips"][2].pop("transition_in")
        sp["profile"] = "punchy"
        report = stage.render_edit(sp, self.settings, preview=True)
        self.assertFalse(report["xfade"])
        planned = T.compile_timeline(E.normalize(sp))["duration"]
        self.assertAlmostEqual(self.probe_duration(report["output"], "v:0"), planned, delta=1.0 / 30 + 0.01)

    def test_missing_source_file(self):
        sp = self.spec("bad")
        sp["sources"]["v"]["file"] = str(Path(self.tmp.name) / "nope.mp4")
        with self.assertRaises(StageError):
            stage.render_edit(sp, self.settings, preview=True)

    def test_missing_sound_file(self):
        sp = self.spec("bad2")
        sp["tracks"]["sfx"][0]["file"] = str(Path(self.tmp.name) / "nope.wav")
        with self.assertRaises(StageError):
            stage.render_edit(sp, self.settings, preview=True)


if __name__ == "__main__":
    unittest.main()
