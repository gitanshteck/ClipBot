"""Tests for clipbot/editscene.py: finding the webcam box and turning samples into spans.

The detector is shown frames with the box in different places, against the edge, and with
things that look a little like one (a cyan blob, a box of another colour, a box too small),
because "it found the box" only means something next to "and nothing else".
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from clipbot import editscene as S, edittimeline as T, editspec as E
from clipbot.utils import StageError

CYAN = (35, 160, 190)
W, H = 960, 540


def frame(box=None, color=CYAN, thick=3, blob=None, bg=(40, 44, 52)):
    """A 960x540 frame of game-coloured noise with an optional border-only box (x, y, w, h as fractions)."""
    rng = np.random.RandomState(7)
    img = np.clip(np.array(bg, dtype=np.int16) + rng.randint(-25, 25, (H, W, 3)), 0, 255).astype(np.uint8)
    if box:
        x0, y0 = int(box[0] * W), int(box[1] * H)
        x1, y1 = min(W, int((box[0] + box[2]) * W)), min(H, int((box[1] + box[3]) * H))
        c = np.array(color, dtype=np.uint8)
        img[y0:y0 + thick, x0:x1] = c
        img[y1 - thick:y1, x0:x1] = c
        img[y0:y1, x0:x0 + thick] = c
        if x1 < W:
            img[y0:y1, x1 - thick:x1] = c
    if blob:
        bx, by, bw, bh = (int(blob[0] * W), int(blob[1] * H), int(blob[2] * W), int(blob[3] * H))
        img[by:by + bh, bx:bx + bw] = np.array(CYAN, dtype=np.uint8)
    return img


def _tools():
    try:
        from clipbot.config import load_settings
        from clipbot.utils import resolve_tool
        return resolve_tool(load_settings().tool("ffmpeg"), "")
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


class TestFindCamBox(unittest.TestCase):
    def assertBox(self, got, want, tol=0.008):
        self.assertIsNotNone(got)
        for got_v, want_v, name in zip((got["x"], got["y"], got["w"], got["h"]), want, "xywh"):
            self.assertAlmostEqual(got_v, want_v, delta=tol, msg=name)

    def test_a_box_top_right(self):
        want = (0.725, 0.109, 0.266, 0.265)
        self.assertBox(S.find_cam_box(frame(want)), want)

    def test_a_box_left_middle(self):
        want = (0.0104, 0.370, 0.267, 0.265)
        self.assertBox(S.find_cam_box(frame(want)), want)

    def test_a_box_pushed_against_the_right_edge_has_no_right_border_to_see(self):
        want = (0.73, 0.11, 0.27, 0.27)         # runs to the frame's edge: x + w = 1.0
        self.assertBox(S.find_cam_box(frame(want)), want)

    def test_no_box_is_none(self):
        self.assertIsNone(S.find_cam_box(frame()))

    def test_a_small_cyan_logo_is_not_a_box(self):
        self.assertIsNone(S.find_cam_box(frame(blob=(0.1, 0.1, 0.05, 0.05))))

    def test_a_box_of_another_colour_is_not_the_webcam(self):
        self.assertIsNone(S.find_cam_box(frame((0.5, 0.2, 0.25, 0.25), color=(200, 40, 40))))

    def test_a_box_too_small_to_be_a_facecam_is_ignored(self):
        self.assertIsNone(S.find_cam_box(frame((0.5, 0.2, 0.06, 0.06))))

    def test_a_border_with_a_wide_hole_in_it_is_still_found(self):
        """Seen in a real render: a thin line over foliage came out as three pieces with holes of 10 and 26 px."""
        want = (0.3, 0.3, 0.27, 0.27)
        img = frame(want)
        top = int(0.3 * H)
        img[top:top + 3, 380:406] = (40, 44, 52)
        self.assertBox(S.find_cam_box(img), want)

    def test_a_washed_out_side_is_enough_when_the_long_lines_are_strong(self):
        """After a zoom and an encode a vertical border measured saturation 0.52; the sides need only SIDE_SAT."""
        want = (0.3, 0.3, 0.27, 0.27)
        img = frame(want)
        x0, y0 = int(0.3 * W), int(0.3 * H)
        x1, y1 = int(0.57 * W), int(0.57 * H)
        pale = np.array((95, 175, 185), dtype=np.uint8)
        img[y0 + 3:y1 - 3, x0:x0 + 3] = pale
        img[y0 + 3:y1 - 3, x1 - 3:x1] = pale
        self.assertBox(S.find_cam_box(img), want)

    def test_a_filled_cyan_area_is_not_a_box(self):
        img = frame()
        img[60:300, 600:900] = np.array(CYAN, dtype=np.uint8)           # a colour bar, a sky: filled, not an outline
        self.assertIsNone(S.find_cam_box(img))

    def test_a_cyan_block_with_a_thin_cyan_line_under_it_is_not_a_box(self):
        """Without the thin-line rule the block reads as a thick 'top' and the underline as the bottom."""
        img = frame()
        img[100:300, 560:900] = np.array(CYAN, dtype=np.uint8)
        img[360:363, 560:900] = np.array(CYAN, dtype=np.uint8)
        self.assertIsNone(S.find_cam_box(img))

    def test_a_tall_narrow_outline_is_not_a_webcam(self):
        self.assertIsNone(S.find_cam_box(frame((0.5, 0.1, 0.16, 0.5))))

    def test_a_box_with_only_two_sides_is_ignored(self):
        img = frame()
        img[60:63, 600:850] = np.array(CYAN, dtype=np.uint8)
        img[200:203, 600:850] = np.array(CYAN, dtype=np.uint8)      # top and bottom, no sides
        self.assertIsNone(S.find_cam_box(img))

    def test_a_border_broken_by_compression_is_still_found(self):
        img = frame((0.3, 0.3, 0.27, 0.27))
        img[int(0.3 * H):int(0.3 * H) + 3, 400:403] = (40, 44, 52)  # a 3 px gap in the top edge
        self.assertIsNotNone(S.find_cam_box(img))


def box(x=0.7, y=0.1, w=0.27, h=0.27):
    return {"x": x, "y": y, "w": w, "h": h}


class TestSpans(unittest.TestCase):
    def test_equal_boxes_are_one_span_covering_the_window(self):
        spans = S.spans_from_samples([(t / 4.0, box(), False) for t in range(8)], 0.0, 2.0)
        self.assertEqual(len(spans), 1)
        self.assertEqual((spans[0]["t0"], spans[0]["t1"]), (0.0, 2.0))
        self.assertEqual(spans[0]["cam"], box())

    def test_a_pixel_of_jitter_is_not_a_new_span_and_the_box_is_the_median(self):
        samples = [(0.0, box(x=0.700), False), (0.25, box(x=0.701), False), (0.5, box(x=0.702), False)]
        spans = S.spans_from_samples(samples, 0.0, 1.0)
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0]["cam"]["x"], 0.701, places=3)

    def test_a_layout_change_is_a_boundary_at_the_first_sample_of_the_new_layout(self):
        samples = [(0.0, box(x=0.7), False), (0.25, box(x=0.7), False), (0.5, box(x=0.01, y=0.37), False), (0.75, box(x=0.01, y=0.37), False)]
        spans = S.spans_from_samples(samples, 0.0, 1.0)
        self.assertEqual([(s["t0"], s["t1"]) for s in spans], [(0.0, 0.5), (0.5, 1.0)])
        self.assertAlmostEqual(spans[1]["cam"]["x"], 0.01, places=3)

    def test_a_box_that_disappears_is_a_span_with_no_box(self):
        spans = S.spans_from_samples([(0.0, box(), False), (0.5, None, False), (1.0, None, False)], 0.0, 1.5)
        self.assertEqual([s["cam"] is None for s in spans], [False, True])

    def test_a_short_miss_between_the_same_box_is_filled(self):
        samples = [(0.0, box(), False), (0.25, None, False), (0.5, box(), False)]
        self.assertEqual(len(S.spans_from_samples(samples, 0.0, 0.75)), 1)

    def test_a_long_miss_is_not_filled(self):
        samples = [(0.0, box(), False), (0.25, None, False), (0.5, None, False), (0.75, box(), False)]
        spans = S.spans_from_samples(samples, 0.0, 1.0)
        self.assertEqual([s["cam"] is None for s in spans], [False, True, False])

    def test_a_miss_between_two_different_boxes_is_not_filled(self):
        samples = [(0.0, box(x=0.7), False), (0.25, None, False), (0.5, box(x=0.01), False)]
        self.assertEqual(len(S.spans_from_samples(samples, 0.0, 0.75)), 3)

    def test_a_box_seen_for_an_instant_between_two_stretches_with_none_is_dropped(self):
        samples = [(0.0, None, False), (0.25, box(), False), (0.5, None, False), (0.75, None, False)]
        spans = S.spans_from_samples(samples, 0.0, 1.0)
        self.assertEqual([s["cam"] for s in spans], [None])

    def test_a_box_that_stays_is_kept(self):
        samples = [(0.0, None, False), (0.25, box(), False), (0.5, box(), False), (0.75, box(), False), (1.0, None, False)]
        spans = S.spans_from_samples(samples, 0.0, 1.25)
        self.assertEqual([s["cam"] is None for s in spans], [True, False, True])

    def test_a_box_at_the_very_start_of_a_clip_is_kept_even_if_short(self):
        samples = [(0.0, box(), False), (0.25, None, False), (0.5, None, False)]
        self.assertEqual([s["cam"] is None for s in S.spans_from_samples(samples, 0.0, 0.75)], [False, True])

    def test_unstable_samples_are_their_own_span_with_no_box(self):
        samples = [(0.0, box(), False), (0.25, None, True), (0.3, None, True), (0.5, box(x=0.01), False)]
        spans = S.spans_from_samples(samples, 0.0, 1.0)
        self.assertEqual(len(spans), 3)
        self.assertTrue(spans[1].get("unstable"))
        self.assertIsNone(spans[1]["cam"])

    def test_no_samples_is_one_span_with_no_box(self):
        self.assertEqual(S.spans_from_samples([], 1.0, 3.0), [{"t0": 1.0, "t1": 3.0, "cam": None}])

    def test_box_at_picks_the_span_and_holds_the_last_past_the_end(self):
        spans = [{"t0": 0.0, "t1": 1.0, "cam": box(x=0.1)}, {"t0": 1.0, "t1": 2.0, "cam": None}]
        self.assertEqual(S.box_at(spans, 0.5)["x"], 0.1)
        self.assertIsNone(S.box_at(spans, 1.0))
        self.assertIsNone(S.box_at(spans, 5.0))


class TestMapToClip(unittest.TestCase):
    def pieces(self, clip):
        return T.build_pieces(E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [clip]})["clips"][0])

    def test_plain_clip_shifts_the_segment_clock_onto_the_clip(self):
        clip = {"id": "a", "src": "v", "in": 100.0, "out": 110.0}
        pieces = self.pieces(clip)
        # the segment file starts 0.6 s before `in`: segment second 0.6 is the clip's first
        spans = [{"t0": 0.0, "t1": 5.6, "cam": box(x=0.1)}, {"t0": 5.6, "t1": 10.6, "cam": box(x=0.5)}]
        got = S.map_to_clip(spans, 99.4, pieces, 100.0, 110.0)
        self.assertEqual([(s["t0"], s["t1"]) for s in got], [(0.0, 5.0), (5.0, 10.0)])

    def test_a_slow_motion_ramp_stretches_the_span_it_covers(self):
        clip = {"id": "a", "src": "v", "in": 0.0, "out": 10.0,
                "speed": [{"at": 4.0, "rate": 1.0}, {"at": 5.0, "rate": 0.5}, {"at": 6.0, "rate": 0.5}, {"at": 7.0, "rate": 1.0}]}
        pieces = self.pieces(clip)
        spans = [{"t0": 0.0, "t1": 5.0, "cam": box(x=0.1)}, {"t0": 5.0, "t1": 10.0, "cam": box(x=0.5)}]
        got = S.map_to_clip(spans, 0.0, pieces, 0.0, 10.0)
        self.assertEqual(len(got), 2)
        self.assertGreater(got[0]["t1"], 5.0)           # the slow part before the boundary takes longer on screen
        self.assertAlmostEqual(got[0]["t1"], got[1]["t0"], places=3)

    def test_neighbouring_spans_with_the_same_box_are_joined(self):
        pieces = self.pieces({"id": "a", "src": "v", "in": 0.0, "out": 4.0})
        spans = [{"t0": 0.0, "t1": 2.0, "cam": box()}, {"t0": 2.0, "t1": 4.0, "cam": box()}]
        self.assertEqual(len(S.map_to_clip(spans, 0.0, pieces, 0.0, 4.0)), 1)


@unittest.skipUnless(_tools(), "ffmpeg is not available")
class TestScanSegment(unittest.TestCase):
    """A real video whose webcam box jumps from the top-right to the left-middle at 1.0 s."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ffmpeg = _tools()
        cls.path = Path(cls._tmp.name) / "scene.mp4"
        c = "0x23A0BE"
        vf = ("drawbox=x=930:y=60:w=510:h=290:color={c}:t=6:enable='lt(t,1)',"
              "drawbox=x=20:y=400:w=510:h=290:color={c}:t=6:enable='gte(t,1)'").format(c=c)
        cls.moved = vf
        subprocess.run([cls.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=2.5",
                        "-vf", vf, "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "15", "-crf", "14", str(cls.path)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_jump_is_found_to_within_a_frame(self):
        spans = S.scan_segment(self.ffmpeg, self.path, 0.0, 2.4)
        self.assertEqual(len(spans), 2, spans)
        self.assertAlmostEqual(spans[0]["cam"]["x"], 930 / 1920.0, delta=0.01)
        self.assertAlmostEqual(spans[1]["cam"]["x"], 20 / 1920.0, delta=0.01)
        self.assertAlmostEqual(spans[1]["t0"], 1.0, delta=1.0 / 30 + 0.01)

    def test_a_window_inside_one_layout_is_one_span(self):
        spans = S.scan_segment(self.ffmpeg, self.path, 0.1, 0.9)
        self.assertEqual(len(spans), 1)
        self.assertIsNotNone(spans[0]["cam"])

    def test_nothing_to_read_is_an_error_not_an_empty_map(self):
        with self.assertRaises(StageError):
            S.scan_segment(self.ffmpeg, self.path, 50.0, 51.0)

    def test_the_scan_is_cached_by_segment_and_window(self):
        with tempfile.TemporaryDirectory() as d:
            settings = mock.Mock(work_root=d)
            a = S.scene_spans(settings, self.ffmpeg, self.path, "segkey", 0.0, 2.0)
            with mock.patch.object(S, "scan_segment", side_effect=AssertionError("scanned twice")):
                b = S.scene_spans(settings, self.ffmpeg, self.path, "segkey", 0.0, 2.0)
            self.assertEqual(a, b)
            self.assertTrue(any(Path(d).rglob("segkey-*.json")))


if __name__ == "__main__":
    unittest.main()
