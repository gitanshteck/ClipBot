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
from unittest import mock

import numpy as np

from clipbot import editaudio, editframe, editqa as Q, editscene, editspec as E, edittimeline as T


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


class TestStaticPicture(unittest.TestCase):
    def test_runs_of_low_motion_are_found(self):
        shares = [0.3] * 8 + [0.001] * 16 + [0.3] * 4 + [0.005] * 6 + [0.3] * 2
        found = Q.stretches_from_shares(shares, 4, 3.0, 0.02)
        self.assertEqual(len(found), 1)                                        # the 1.5 s stretch is too short
        self.assertAlmostEqual(found[0]["start"], 2.0)
        self.assertAlmostEqual(found[0]["dur"], 4.0)
        self.assertLess(found[0]["moving"], 0.01)

    def test_busy_picture_has_none(self):
        self.assertEqual(Q.stretches_from_shares([0.2] * 40, 4, 3.0, 0.02), [])

    def test_a_stretch_running_to_the_end_is_kept(self):
        found = Q.stretches_from_shares([0.3] * 4 + [0.0] * 20, 4, 3.0, 0.02)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0]["end"], 6.0)


class TestStaticPictureOnMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()
        if not cls.tools:
            raise unittest.SkipTest("ffmpeg is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ff = cls.tools[0]
        d = Path(cls.tmp.name)
        # 4 s of motion, 5 s of a still frame with a tiny moving dot (a webcam's worth), 4 s of motion
        cls.mixed = d / "mixed.mp4"
        subprocess.run([cls.ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
                        "-f", "lavfi", "-i", "color=c=0x303030:size=320x180:rate=30:duration=5",
                        "-f", "lavfi", "-i", "color=c=white:size=14x14:rate=30:duration=5",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
                        "-filter_complex", "[1:v][2:v]overlay=x='20+40*sin(t*5)':y=20[mid];"
                                           "[0:v][mid][3:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.mixed)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_a_still_scene_with_a_small_moving_part_is_dead_picture(self):
        found = Q.static_stretches(self.ff, self.mixed)
        self.assertEqual(len(found), 1, found)
        self.assertAlmostEqual(found[0]["start"], 4.0, delta=0.6)
        self.assertGreater(found[0]["dur"], 4.0)

    def test_the_freeze_detector_alone_would_have_missed_it(self):
        self.assertEqual(Q.frozen_frames(self.ff, self.mixed), [])


class TestJoinJumps(unittest.TestCase):
    RATE = 16000

    def tone(self, seconds, amp):
        t = np.arange(int(seconds * self.RATE)) / float(self.RATE)
        return amp * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 5 * t))

    def timeline(self, *clips):
        t, out = 0.0, []
        for cid, kind, dur in clips:
            out.append({"id": cid, "type": kind, "start": t, "end": t + dur})
            t += dur
        return {"clips": out, "duration": t}

    def test_a_step_across_a_join_is_flagged_with_its_size_and_sign(self):
        samples = np.concatenate([self.tone(4, 0.4), self.tone(4, 0.04), self.tone(4, 0.04)])
        tl = self.timeline(("a", "clip", 4), ("b", "clip", 4), ("c", "clip", 4))
        jumps = Q.join_jumps(samples, self.RATE, tl)
        self.assertEqual([j["clip"] for j in jumps], ["b"])
        self.assertAlmostEqual(jumps[0]["jump_db"], -20.0, delta=2.0)

    def test_matched_levels_are_clean(self):
        samples = np.concatenate([self.tone(4, 0.3), self.tone(4, 0.25), self.tone(4, 0.3)])
        tl = self.timeline(("a", "clip", 4), ("b", "clip", 4), ("c", "clip", 4))
        self.assertEqual(Q.join_jumps(samples, self.RATE, tl), [])

    def test_a_card_may_change_the_sound(self):
        samples = np.concatenate([self.tone(4, 0.4), self.tone(3, 0.001), self.tone(4, 0.4)])
        tl = self.timeline(("a", "clip", 4), ("title", "card", 3), ("b", "clip", 4))
        self.assertEqual(Q.join_jumps(samples, self.RATE, tl), [])

    def test_worst_first(self):
        samples = np.concatenate([self.tone(4, 0.4), self.tone(4, 0.1), self.tone(4, 0.002)])
        tl = self.timeline(("a", "clip", 4), ("b", "clip", 4), ("c", "clip", 4))
        jumps = Q.join_jumps(samples, self.RATE, tl)
        self.assertEqual([j["clip"] for j in jumps], ["c", "b"])


def _timeline(*clips):
    """A minimal compiled timeline: clips are (id, start, in, out)."""
    ents = []
    for cid, start, a, b in clips:
        spec = E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [{"id": cid, "src": "v", "in": a, "out": b}]})
        pieces = T.build_pieces(spec["clips"][0])
        ents.append({"id": cid, "type": "clip", "start": start, "in": a, "out": b, "dur": b - a, "pieces": pieces})
    return {"clips": ents}


def _w(text, s, e):
    return {"w": text, "s": s, "e": e, "vs": s, "ve": e}


class TestWordChecks(unittest.TestCase):
    words = {"a": [_w("main", 10.0, 10.4), _w("nahi", 10.5, 10.9), _w("chudi", 11.0, 11.6)],
             "b": [_w("fuck", 50.0, 50.4), _w("it", 50.5, 50.7)]}

    def test_an_edge_inside_a_word_is_reported_with_the_move_that_would_clear_it(self):
        tl = _timeline(("a", 0.0, 10.7, 11.3))
        bad = Q.cuts_inside_words(tl, self.words)
        self.assertEqual([(b["kind"], b["word"]) for b in bad], [("in", "nahi"), ("out", "chudi")])
        self.assertLess(bad[0]["fix"], 0)
        self.assertGreater(bad[1]["fix"], 0)

    def test_edges_in_gaps_are_clean_and_a_clip_without_words_is_not_judged(self):
        tl = _timeline(("a", 0.0, 10.45, 10.95), ("z", 20.0, 5.0, 8.0))
        self.assertEqual(Q.cuts_inside_words(tl, self.words), [])

    def test_strong_words_are_found_where_they_are_heard_in_the_finished_video(self):
        tl = _timeline(("a", 0.0, 10.0, 12.0), ("b", 100.0, 49.0, 52.0))
        hits = Q.strong_language(tl, self.words)
        self.assertEqual([(h["clip"], h["word"]) for h in hits], [("b", "fuck")])
        self.assertAlmostEqual(hits[0]["t"], 101.0, places=1)              # clip b starts at 100 s, "fuck" is 1 s into it

    def test_a_strong_word_outside_the_clips_window_is_not_in_the_video(self):
        tl = _timeline(("b", 0.0, 51.0, 53.0))
        self.assertEqual(Q.strong_language(tl, self.words), [])

    def test_a_strong_word_early_is_found_early(self):
        tl = _timeline(("b", 5.0, 49.0, 52.0))
        self.assertLess(Q.strong_language(tl, self.words)[0]["t"], 30.0)

    def test_strong_language_follows_the_retiming(self):
        spec = E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [
            {"id": "b", "src": "v", "in": 48.0, "out": 53.0,
             "speed": [{"at": 49.0, "rate": 1.0}, {"at": 49.5, "rate": 0.5}, {"at": 51.0, "rate": 0.5}, {"at": 51.5, "rate": 1.0}]}]})
        tl = {"clips": [{"id": "b", "type": "clip", "start": 0.0, "in": 48.0, "out": 53.0, "dur": 7.0, "pieces": T.build_pieces(spec["clips"][0])}]}
        plain = Q.strong_language(_timeline(("b", 0.0, 48.0, 53.0)), self.words)[0]["t"]
        slowed = Q.strong_language(tl, self.words)[0]["t"]
        self.assertGreater(slowed, plain)                                  # the slow-motion before it pushes it later in the video


class TestWebcamSampleFrames(unittest.TestCase):
    """`webcam_in_frame` judges a sample on the few frames of its 0.15 s: a smeared border on one frame of a low-quality
    encode is not a missing webcam, but a box in the wrong place on every frame still is."""

    BOX = {"t0": 0.0, "t1": 3.0, "cam": {"x": 0.70, "y": 0.10, "w": 0.28, "h": 0.28}}

    def entry(self):
        return {"id": "c1", "type": "clip", "start": 10.0, "dur": 3.0, "scene": [dict(self.BOX)],
                "camera": [{"t": 0.0, "zoom": 1.0, "x": 0.5, "y": 0.5, "ease": "linear"}, {"t": 1.0, "zoom": 1.3, "x": 0.5, "y": 0.5, "ease": "linear"},
                           {"t": 3.0, "zoom": 1.3, "x": 0.5, "y": 0.5, "ease": "linear"}]}

    def run_with(self, finds):
        """`finds` is the detector's answer per frame: None, 'right' or 'wrong'."""
        entry = self.entry()
        z, x, y = editframe.camera_at(entry["camera"], 1.5)
        right = editframe.box_in_output(editscene.box_at(entry["scene"], 1.5), z, x, y)
        wrong = dict(right, x=right["x"] + 0.2)
        answers = iter(finds)
        frames = [(0.0, object()) for _ in finds]
        with mock.patch.object(editscene, "read_frames", return_value=iter(frames)), \
                mock.patch.object(editscene, "find_cam_box", side_effect=lambda f: {"right": right, "wrong": wrong, None: None}[next(answers)]), \
                mock.patch.object(Q, "_webcam_times", return_value=[1.5]):
            return Q.webcam_in_frame("ffmpeg", "x.mp4", {"clips": [entry]}, per_clip=1)

    def test_a_box_smeared_on_the_first_frames_but_found_on_a_later_one_is_a_match(self):
        rows = self.run_with([None, None, "right"])
        self.assertEqual((rows[0]["checked"], rows[0]["matched"]), (1, 1))

    def test_a_box_that_is_never_found_is_a_miss(self):
        rows = self.run_with([None, None, None, None])
        self.assertEqual((rows[0]["checked"], rows[0]["matched"]), (1, 0))

    def test_a_box_in_the_wrong_place_on_every_frame_is_a_miss_and_says_where_it_was_found(self):
        rows = self.run_with(["wrong", "wrong", "wrong"])
        self.assertEqual((rows[0]["checked"], rows[0]["matched"]), (1, 0))
        self.assertIsNotNone(rows[0]["misses"][0][1])

    def test_a_clip_with_no_frames_to_read_is_not_counted(self):
        self.assertEqual(self.run_with([]), [])

    def test_each_frame_is_judged_against_where_the_camera_is_at_that_frame(self):
        entry = self.entry()
        entry["camera"] = [{"t": 0.0, "zoom": 1.1, "x": 0.5, "y": 0.5, "ease": "linear"}, {"t": 1.5, "zoom": 1.1, "x": 0.5, "y": 0.5, "ease": "linear"},
                           {"t": 1.7, "zoom": 1.9, "x": 0.5, "y": 0.5, "ease": "linear"}, {"t": 3.0, "zoom": 1.9, "x": 0.5, "y": 0.5, "ease": "linear"}]
        cam = editscene.box_at(entry["scene"], 1.5)
        early = editframe.box_in_output(cam, *editframe.camera_at(entry["camera"], 1.5))
        late = editframe.box_in_output(cam, *editframe.camera_at(entry["camera"], 1.5 + 3 / 30.0))
        self.assertGreater(abs(late["x"] - early["x"]), Q.WEBCAM_TOL)          # the zoom really moves the box in 3 frames
        answers = iter([None, None, None, late])
        with mock.patch.object(editscene, "read_frames", return_value=iter([(0.0, object())] * 4)), \
                mock.patch.object(editscene, "find_cam_box", side_effect=lambda f: next(answers)), \
                mock.patch.object(Q, "_webcam_times", return_value=[1.5]):
            rows = Q.webcam_in_frame("ffmpeg", "x.mp4", {"clips": [entry]}, per_clip=1)
        self.assertEqual((rows[0]["checked"], rows[0]["matched"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
