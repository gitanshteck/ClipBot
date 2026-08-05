"""Hinglish captions overlay: spec validation, geometry, and the invariant
that a captions-off clip's filter graph/argv is unaffected by the feature
existing at all - same discipline test_reelspec_invariant.py already applies
to fx and chat.

Run: python -m unittest discover -s tests
"""

import unittest

from clipbot import captionrender
from clipbot import reelspec as R
from clipbot.specerror import SpecError


def _spec(captions=None):
    spec = {"preset": "cam_top", "canvas": "1080x1920",
            "src": {"cam": dict(R.DEFAULT_CAM)}}
    if captions is not None:
        spec["captions"] = captions
    return spec


class TestCaptionsBlockValidation(unittest.TestCase):
    def test_disabled_or_missing_is_omitted(self):
        self.assertNotIn("captions", R.normalize(_spec()))
        self.assertNotIn("captions", R.normalize(_spec({"enabled": False})))

    def test_defaults_fill_in(self):
        out = R.normalize(_spec({}))["captions"]
        self.assertEqual(out["position"], "bottom")
        self.assertEqual(out["max_lines"], 2)
        self.assertIsNone(out["offset"])

    def test_bad_position_rejected(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"position": "left"}))

    def test_size_out_of_range_rejected(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"size": 0.9}))

    def test_max_lines_out_of_range_rejected(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"max_lines": 0}))
        with self.assertRaises(SpecError):
            R.normalize(_spec({"max_lines": 9}))

    def test_offset_must_be_numeric_or_null(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"offset": "soon"}))
        out = R.normalize(_spec({"offset": 1.5}))["captions"]
        self.assertEqual(out["offset"], 1.5)

    def test_style_must_be_object(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"style": "bold"}))

    def test_style_drops_unknown_keys(self):
        out = R.normalize(_spec({"style": {"font_size": 60, "nonsense": 1}}))["captions"]
        self.assertEqual(out["style"], {"font_size": 60})


class TestCaptionsResolve(unittest.TestCase):
    def test_rect_is_full_width_and_anchored(self):
        spec = _spec({"position": "bottom", "size": 0.2})
        plan = R.resolve(spec, 1280, 720)
        full_w, full_h = plan["canvas"]
        x, y, w, h = plan["captions"]["rect"]
        self.assertEqual((x, w), (0, full_w))
        self.assertEqual(y + h, full_h)  # flush with the bottom edge

    def test_top_position_anchors_at_zero(self):
        plan = R.resolve(_spec({"position": "top", "size": 0.2}), 1280, 720)
        x, y, w, h = plan["captions"]["rect"]
        self.assertEqual(y, 0)

    def test_no_captions_key_when_absent(self):
        plan = R.resolve(_spec(), 1280, 720)
        self.assertNotIn("captions", plan)


class TestCaptionsLegacyPathUnaffected(unittest.TestCase):
    """The exact invariant test_reelspec_invariant.py applies to fx: enabling
    a new overlay must not perturb a clip that doesn't use it."""

    def test_build_filter_ignores_none_caption_input(self):
        plan = R.resolve(_spec(), 1280, 720)
        self.assertEqual(
            R.build_filter(plan),
            R.build_filter(plan, caption_input=None),
        )

    def test_build_argv_ignores_none_caption_list(self):
        plan = R.resolve(_spec(), 1280, 720)
        base = R.build_argv("ffmpeg", "SRC.mp4", "OUT.mp4", 10.0, 45.0, plan)
        self.assertEqual(
            base,
            R.build_argv("ffmpeg", "SRC.mp4", "OUT.mp4", 10.0, 45.0, plan,
                         caption_list=None),
        )

    def test_captions_enabled_forces_the_fx_aware_path(self):
        # Not the legacy path's job to know about captions - it must route
        # through _build_filter_fx even with fx_plan=None, same as chat does.
        plan = R.resolve(_spec({"position": "bottom"}), 1280, 720)
        graph = R.build_filter(plan, caption_input=1)
        self.assertIn("[1:v]setpts=PTS-STARTPTS,format=rgba[cap]", graph)
        self.assertIn("format=yuv420p[v]", graph)

    def test_overlay_input_index_orders_chat_then_captions(self):
        self.assertEqual(
            R.overlay_input_index(None, None),
            {"chat": None, "captions": None, "next": 1},
        )
        self.assertEqual(
            R.overlay_input_index("chat.txt", None),
            {"chat": 1, "captions": None, "next": 2},
        )
        self.assertEqual(
            R.overlay_input_index(None, "cap.txt"),
            {"chat": None, "captions": 1, "next": 2},
        )
        self.assertEqual(
            R.overlay_input_index("chat.txt", "cap.txt"),
            {"chat": 1, "captions": 2, "next": 3},
        )


class TestCaptionRenderFrames(unittest.TestCase):
    def test_no_segments_returns_none(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            style = captionrender.resolve_style(None)
            result = captionrender.render_frames([], 1080, 300, 10.0, Path(tmp), style)
            self.assertIsNone(result)

    def test_segments_outside_window_are_dropped(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            style = captionrender.resolve_style(None)
            segments = [{"id": 0, "start": 100.0, "end": 105.0, "text": "kya baat hai"}]
            result = captionrender.render_frames(
                segments, 1080, 300, 10.0, Path(tmp), style
            )
            self.assertIsNone(result)

    def test_real_segments_render_frames_and_ffconcat(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            style = captionrender.resolve_style(None)
            segments = [
                {"id": 0, "start": 1.0, "end": 3.0, "text": "kya baat hai bhai"},
                {"id": 1, "start": 3.0, "end": 5.5, "text": "sahi mein ekdum mast"},
            ]
            out_dir = Path(tmp)
            result = captionrender.render_frames(segments, 1080, 300, 8.0, out_dir, style)
            self.assertIsNotNone(result)
            self.assertTrue(result.exists())
            frames = list(out_dir.glob("f_*.png"))
            self.assertGreaterEqual(len(frames), 2)  # at least the two caption spans
            listing = result.read_text(encoding="utf-8")
            self.assertIn("ffconcat version 1.0", listing)


if __name__ == "__main__":
    unittest.main()
