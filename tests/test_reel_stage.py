"""Tests for the filtergraph-file handoff in clipbot/stages/reel.py.

`_maybe_script_filter` moves an oversized `-filter_complex` into a file. The flag
that points ffmpeg at that file is the whole point: `-filter_complex_script` was
removed in ffmpeg 8 (the vendored build), and an argv carrying it fails at once,
only for a reel whose graph is long enough to cross the threshold. The real-ffmpeg
test below runs the produced argv end to end, because a string comparison would
have passed against the broken flag too.

Run: python -m unittest discover -s tests
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipbot import reelspec as R
from clipbot.config import Settings, load_settings
from clipbot.stages import reel
from clipbot.utils import resolve_tool
from clipbot.workspace import Workspace


def _tools():
    try:
        s = load_settings()
        return resolve_tool(s.tool("ffmpeg"), ""), resolve_tool(s.tool("ffprobe"), "")
    except Exception:  # noqa: BLE001
        w = shutil.which("ffmpeg"), shutil.which("ffprobe")
        return w if all(w) else None


def _argv_with_graph(graph):
    return ["ffmpeg", "-y", "-i", "in.mp4", "-t", "1.000", "-filter_complex", graph,
            "-map", "[v]", "out.mp4"]


class TestMaybeScriptFilter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws").ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def test_short_graph_is_left_alone(self):
        argv = _argv_with_graph("[0:v]null[v]")
        out, sig = reel._maybe_script_filter(argv, self.ws, "c1", dry_run=False)
        self.assertEqual(out, argv)
        self.assertEqual(sig, "")
        self.assertFalse((self.ws.logs_dir / "fx-c1.filter").exists())

    def test_argv_without_a_graph_is_left_alone(self):
        argv = ["ffmpeg", "-i", "in.mp4", "out.mp4"]
        self.assertEqual(reel._maybe_script_filter(argv, self.ws, "c1", False), (argv, ""))

    def test_long_graph_moves_to_a_file_with_the_ffmpeg_8_flag(self):
        graph = "[0:v]" + ",".join(["null"] * 2500) + "[v]"
        self.assertGreater(len(graph), reel.FILTER_SCRIPT_THRESHOLD)
        argv = _argv_with_graph(graph)
        out, sig = reel._maybe_script_filter(argv, self.ws, "c1", dry_run=False)

        path = self.ws.logs_dir / "fx-c1.filter"
        self.assertEqual(path.read_text(encoding="utf-8"), graph)
        self.assertNotIn("-filter_complex_script", out)
        self.assertNotIn("-filter_complex", out)
        at = out.index("-/filter_complex")
        self.assertEqual(out[at + 1], str(path))
        # Only that pair changed: everything around it is byte-identical.
        self.assertEqual(out[:at], argv[:argv.index("-filter_complex")])
        self.assertEqual(out[at + 2:], argv[argv.index("-filter_complex") + 2:])
        self.assertEqual(len(sig), 16)

    def test_signature_follows_the_graph_not_the_filename(self):
        a = "[0:v]" + ",".join(["null"] * 2500) + "[v]"
        b = "[0:v]" + ",".join(["null"] * 2501) + "[v]"
        out_a, sig_a = reel._maybe_script_filter(_argv_with_graph(a), self.ws, "c1", False)
        out_b, sig_b = reel._maybe_script_filter(_argv_with_graph(b), self.ws, "c1", False)
        self.assertEqual(out_a, out_b)  # same stable filename, same argv...
        self.assertNotEqual(sig_a, sig_b)  # ...so the signature carries the change

    def test_dry_run_writes_nothing(self):
        graph = "[0:v]" + ",".join(["null"] * 2500) + "[v]"
        out, _ = reel._maybe_script_filter(_argv_with_graph(graph), self.ws, "c1", dry_run=True)
        self.assertIn("-/filter_complex", out)
        self.assertFalse((self.ws.logs_dir / "fx-c1.filter").exists())


@unittest.skipUnless(_tools(), "ffmpeg not available")
class TestRealFfmpeg(unittest.TestCase):
    """The produced argv has to be something the vendored ffmpeg accepts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ffmpeg, self.ffprobe = _tools()
        self.ws = Workspace(self.tmp / "ws").ensure()
        self.source = self.tmp / "src.mp4"
        subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(self.source)],
            check=True,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _reel_argv(self, out):
        settings = Settings({"reel": {"preset_x264": "ultrafast", "crf": 30}}, Path("s.json"))
        spec = {"preset": "cam_top", "canvas": "270x480", "src": {"cam": dict(R.DEFAULT_CAM)}}
        plan = R.resolve(spec, 1280, 720, settings)
        return R.build_argv(
            self.ffmpeg, self.source, out, 0.0, 1.0, plan, settings, progress_pipe=False,
        )

    def _probe(self, path):
        proc = subprocess.run(
            [self.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        )
        return json.loads(proc.stdout)["streams"][0]

    def test_a_graph_in_a_file_renders(self):
        out = self.tmp / "reel.mp4"
        argv = self._reel_argv(out)
        with mock.patch.object(reel, "FILTER_SCRIPT_THRESHOLD", 1):
            argv, sig = reel._maybe_script_filter(argv, self.ws, "c1", dry_run=False)
        self.assertIn("-/filter_complex", argv)
        self.assertTrue(sig)

        proc = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stream = self._probe(out)
        self.assertEqual((stream["width"], stream["height"]), (270, 480))

    def test_file_and_inline_graphs_render_the_same_picture(self):
        inline_out = self.tmp / "inline.mp4"
        file_out = self.tmp / "file.mp4"
        inline = subprocess.run(self._reel_argv(inline_out), capture_output=True, text=True)
        self.assertEqual(inline.returncode, 0, inline.stderr)

        with mock.patch.object(reel, "FILTER_SCRIPT_THRESHOLD", 1):
            argv, _ = reel._maybe_script_filter(
                self._reel_argv(file_out), self.ws, "c1", dry_run=False)
        filed = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(filed.returncode, 0, filed.stderr)
        self.assertEqual(self._probe(inline_out), self._probe(file_out))

        def frame_hashes(path):
            proc = subprocess.run(
                [self.ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0",
                 "-f", "framemd5", "-"],
                capture_output=True, text=True, check=True,
            )
            return [ln.split(",")[-1].strip() for ln in proc.stdout.splitlines()
                    if ln and not ln.startswith("#")]

        self.assertEqual(frame_hashes(inline_out), frame_hashes(file_out))


if __name__ == "__main__":
    unittest.main()
