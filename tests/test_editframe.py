"""Tests for clipbot/editframe.py: camera moves that never crop the webcam.

The central claim is an invariant - at every sampled second the webcam box is inside the
camera's window - so it is checked on a fine time grid, over layouts taken from the real
streams and over randomly placed boxes, and shown to FAIL for the centred zoom it replaces.
"""

import random
import unittest

from clipbot import editframe as F, editspec as E

YT = {"x": 0.725, "y": 0.109, "w": 0.266, "h": 0.265}          # top-right, a hair from the edge
KICK = {"x": 0.0104, "y": 0.370, "w": 0.267, "h": 0.265}       # left-middle, a hair from the edge


def key(t, zoom, x=0.5, y=0.5, ease="inout2"):
    return {"t": t, "zoom": zoom, "x": x, "y": y, "ease": ease}


def push(top=1.09, dur=5.0):
    """A slow push-in that holds and eases back: the shape of an ambient move."""
    return [key(0.0, 1.0), key(dur * 0.4, top), key(dur * 0.7, top, ease="linear"), key(dur, 1.0)]


def span(box, t0=0.0, t1=5.0, **kw):
    return dict({"t0": t0, "t1": t1, "cam": box}, **kw)


class TestGeometry(unittest.TestCase):
    def test_the_window_of_a_centred_zoom(self):
        x0, y0, x1, y1 = F.window(2.0, 0.5, 0.5)
        self.assertEqual((x0, y0, x1, y1), (0.25, 0.25, 0.75, 0.75))

    def test_the_window_slides_to_the_edge_rather_than_past_it(self):
        x0, _, x1, _ = F.window(2.0, 0.95, 0.5)
        self.assertEqual((x0, x1), (0.5, 1.0))

    def test_no_zoom_shows_the_whole_frame(self):
        self.assertEqual(F.window(1.0, 0.3, 0.9), (0.0, 0.0, 1.0, 1.0))

    def test_where_the_box_lands_in_the_output(self):
        r = F.box_in_output({"x": 0.5, "y": 0.5, "w": 0.25, "h": 0.25}, 2.0, 0.5, 0.5)
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (0.5, 0.5, 0.5, 0.5))

    def test_camera_at_follows_the_later_keys_ease_and_holds_outside(self):
        keys = [key(1.0, 1.0), key(3.0, 1.5, ease="out2")]
        self.assertEqual(F.camera_at(keys, 0.0)[0], 1.0)
        self.assertEqual(F.camera_at(keys, 9.0)[0], 1.5)
        p = E.ease_value("out2", 0.25)
        self.assertAlmostEqual(F.camera_at(keys, 1.5)[0], 1.0 + 0.5 * p, places=9)

    def test_camera_at_with_no_keys_is_the_whole_frame(self):
        self.assertEqual(F.camera_at([], 1.0), (1.0, 0.5, 0.5))


class TestSafeView(unittest.TestCase):
    def test_a_centred_zoom_crops_a_box_on_the_right_edge(self):
        self.assertFalse(F.box_in_window(YT, 1.09, 0.5, 0.5))

    def test_the_safe_view_keeps_it_with_a_margin(self):
        z, x, y = F.safe_view(1.09, 0.5, 0.5, YT)
        self.assertEqual(z, 1.09)
        x0, y0, x1, y1 = F.window(z, x, y)
        self.assertGreaterEqual(x1, YT["x"] + YT["w"] + F.MARGIN - 1e-9)
        self.assertLessEqual(y0, YT["y"] - F.MARGIN + 1e-9)

    def test_it_moves_the_pan_only_as_far_as_it_must(self):
        _, x, y = F.safe_view(1.09, 0.5, 0.5, YT)
        self.assertEqual(y, 0.5)                       # the box is already in view vertically: untouched
        self.assertGreater(x, 0.5)
        self.assertLess(x, 0.56)                       # a few percent, not a swing to the corner

    def test_a_box_already_in_view_changes_nothing(self):
        mid = {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}
        self.assertEqual(F.safe_view(1.09, 0.5, 0.5, mid), (1.09, 0.5, 0.5))

    def test_left_edge_box_pans_left(self):
        _, x, _ = F.safe_view(1.09, 0.5, 0.5, KICK)
        self.assertLess(x, 0.5)

    def test_no_zoom_or_no_box_is_left_alone(self):
        self.assertEqual(F.safe_view(1.0, 0.3, 0.3, YT), (1.0, 0.3, 0.3))
        self.assertEqual(F.safe_view(1.5, 0.3, 0.3, None), (1.5, 0.3, 0.3))

    def test_a_box_too_big_for_the_window_caps_the_zoom(self):
        big = {"x": 0.1, "y": 0.1, "w": 0.7, "h": 0.5}
        z, x, y = F.safe_view(1.6, 0.5, 0.5, big)
        self.assertLess(z, 1.6)
        self.assertTrue(F.box_in_window(big, z, x, y))

    def test_a_box_that_fills_the_frame_allows_no_zoom(self):
        z, _, _ = F.safe_view(1.5, 0.5, 0.5, {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})
        self.assertEqual(z, 1.0)


class TestSafeCamera(unittest.TestCase):
    def test_the_centred_push_crops_the_webcam_and_the_safe_one_does_not(self):
        keys, spans = push(), [span(YT)]
        self.assertTrue(F.violations(keys, spans))                       # the old behaviour: shown to fail
        fixed, notes = F.safe_camera(keys, spans)
        self.assertEqual(F.violations(fixed, spans), [])
        self.assertEqual(notes, [])

    def test_both_real_layouts_and_a_punch(self):
        punch = [key(0.0, 1.0), key(1.0, 1.0, ease="out2"), key(1.2, 1.12, ease="out2"), key(1.7, 1.12, ease="linear"), key(2.4, 1.0)]
        for box in (YT, KICK):
            for keys in (push(), punch, push(1.3)):
                fixed, _ = F.safe_camera(keys, [span(box)])
                self.assertEqual(F.violations(fixed, [span(box)]), [], (box, keys))

    def random_case(self, rng, flush):
        w, h = rng.uniform(0.1, 0.4), rng.uniform(0.1, 0.4)
        gap = 0.0 if flush else F.MARGIN
        box = {"x": rng.uniform(gap, 1 - w - gap), "y": rng.uniform(gap, 1 - h - gap), "w": w, "h": h}
        keys = [key(0.0, 1.0)]
        t = 0.0
        for _ in range(rng.randint(1, 5)):
            t += rng.uniform(0.3, 2.0)
            keys.append(key(t, rng.uniform(1.0, 1.3), rng.uniform(0.2, 0.8), rng.uniform(0.2, 0.8), rng.choice(E.EASES)))
        return box, keys, [span(box, 0.0, t + 1.0)]

    def test_random_boxes_zooms_and_keys_never_lose_the_box(self):
        """A box with the margin's worth of frame around it (every real layout) stays whole, exactly."""
        rng = random.Random(11)
        for _ in range(400):
            box, keys, sp = self.random_case(rng, flush=False)
            fixed, _ = F.safe_camera(keys, sp)
            self.assertEqual(F.violations(fixed, sp, tol=0.0), [], (box, keys))

    def test_a_box_flush_with_the_source_edge_loses_no_more_than_a_few_pixels(self):
        """Nothing can hold a box exactly at the edge while the zoom eases; the loss is bounded (TOLERANCE, 3 px at 1080p)."""
        rng = random.Random(12)
        for _ in range(400):
            box, keys, sp = self.random_case(rng, flush=True)
            fixed, _ = F.safe_camera(keys, sp)
            self.assertEqual(F.violations(fixed, sp), [], (box, keys))

    def test_zoom_is_never_raised_and_never_below_one(self):
        fixed, _ = F.safe_camera(push(1.2), [span(YT)])
        self.assertTrue(all(1.0 <= k["zoom"] <= 1.2 + 1e-9 for k in fixed))

    def test_no_zoom_no_change(self):
        keys = [key(0.0, 1.0), key(2.0, 1.0)]
        self.assertEqual(F.safe_camera(keys, [span(YT)])[0], keys)

    def test_no_spans_no_change(self):
        keys = push()
        self.assertEqual(F.safe_camera(keys, [])[0], keys)

    def test_full_cam_scene_with_no_box_keeps_the_authored_centre(self):
        keys = push()
        fixed, notes = F.safe_camera(keys, [span(None)])
        self.assertEqual([(k["x"], k["y"]) for k in fixed], [(0.5, 0.5)] * len(keys))
        self.assertEqual(notes, [])

    def test_a_layout_switch_inside_a_zoom_moves_the_pan_with_it(self):
        keys = [key(0.0, 1.0), key(1.0, 1.09), key(5.0, 1.09, ease="linear"), key(6.0, 1.0)]
        spans = [span(YT, 0.0, 3.0), span(KICK, 3.0, 6.0)]
        fixed, notes = F.safe_camera(keys, spans, fps=60.0)
        self.assertEqual(F.violations(fixed, spans), [])
        self.assertGreater(len(fixed), len(keys))                          # keys were added at the switch
        before, after = F.camera_at(fixed, 3.0 - 0.002), F.camera_at(fixed, 3.0)
        self.assertGreater(before[1], 0.5)                                 # still panned for the right-hand box
        self.assertLess(after[1], 0.5)                                     # and panned left for the new one
        self.assertAlmostEqual(before[0], after[0], places=2)              # the zoom itself does not jump

    def test_a_switch_while_not_zoomed_adds_nothing(self):
        keys = [key(0.0, 1.0), key(1.0, 1.0), key(4.0, 1.09), key(5.0, 1.0)]
        spans = [span(YT, 0.0, 0.5), span(KICK, 0.5, 5.0)]
        fixed, _ = F.safe_camera(keys, spans)
        self.assertEqual(len(fixed), len(keys))

    def test_zoom_through_a_sliding_webcam_is_reported(self):
        keys = [key(0.0, 1.0), key(1.0, 1.09), key(4.0, 1.09, ease="linear"), key(5.0, 1.0)]
        spans = [span(YT, 0.0, 2.0), span(None, 2.0, 2.5, unstable=True), span(KICK, 2.5, 5.0)]
        _, notes = F.safe_camera(keys, spans)
        self.assertTrue(any("moving between layouts" in n for n in notes), notes)

    def test_a_cap_is_reported(self):
        big = {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.7}
        _, notes = F.safe_camera(push(1.5), [span(big)])
        self.assertTrue(any("limited" in n for n in notes), notes)

    def test_the_input_keys_are_not_mutated(self):
        keys = push()
        before = [dict(k) for k in keys]
        F.safe_camera(keys, [span(YT)])
        self.assertEqual(keys, before)


class TestOverlayCollisions(unittest.TestCase):
    canvas = (1920, 1080)

    def overlay(self, **kw):
        return dict({"label": "THE BASE", "t0": 1.0, "t1": 3.0, "x": 0.5, "y": 0.10, "anchor": "center", "w": 700, "h": 90}, **kw)

    def test_a_wide_title_at_the_top_centre_runs_into_a_top_right_webcam(self):
        hits = F.overlay_collisions([self.overlay(w=1100)], [], [span(YT, 0.0, 5.0)], self.canvas)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["label"], "THE BASE")

    def test_a_narrower_title_clears_it(self):
        self.assertEqual(F.overlay_collisions([self.overlay(w=700)], [], [span(YT, 0.0, 5.0)], self.canvas), [])

    def test_a_callout_at_the_bottom_centre_clears_both_layouts(self):
        o = self.overlay(y=0.80, w=900, h=160)
        for box in (YT, KICK):
            self.assertEqual(F.overlay_collisions([o], [], [span(box, 0.0, 5.0)], self.canvas), [])

    def test_no_webcam_no_collision(self):
        self.assertEqual(F.overlay_collisions([self.overlay(w=1900)], [], [span(None, 0.0, 5.0)], self.canvas), [])

    def test_the_zoom_moves_the_webcam_under_an_overlay(self):
        # a label on the right clears a box left of it at zoom 1.0; the picture growing pushes the box into it
        o = self.overlay(anchor="left", x=0.88, y=0.20, w=int(0.1 * 1920))
        mid = {"x": 0.6, "y": 0.1, "w": 0.25, "h": 0.25}
        clear = F.overlay_collisions([o], [], [span(mid, 0.0, 5.0)], self.canvas)
        keys = [key(0.0, 1.5, 0.5, 0.5, "linear"), key(5.0, 1.5, 0.5, 0.5, "linear")]
        zoomed = F.overlay_collisions([o], keys, [span(mid, 0.0, 5.0)], self.canvas)
        self.assertEqual(clear, [])
        self.assertEqual(len(zoomed), 1)

    def test_the_hit_reports_the_seconds_it_lasts(self):
        o = self.overlay(w=1200, t0=1.0, t1=2.0)
        hit = F.overlay_collisions([o], [], [span(YT, 0.0, 5.0)], self.canvas)[0]
        self.assertAlmostEqual(hit["t0"], 1.0, places=2)
        self.assertAlmostEqual(hit["t1"], 2.0, places=2)


class TestClearOfWebcam(unittest.TestCase):
    canvas = (1920, 1080)

    def overlay(self, **kw):
        return dict({"label": "T", "t0": 1.0, "t1": 3.0, "x": 0.5, "y": 0.10, "anchor": "center", "w": 700, "h": 90}, **kw)

    def moved(self, o, keys=(), spans=None):
        move = F.clear_of_webcam(o, keys, spans or [span(YT, 0.0, 5.0)], self.canvas)
        self.assertIsNotNone(move)
        o2 = dict(o, x=o["x"] + move["dx"] / self.canvas[0], y=o["y"] + move["dy"] / self.canvas[1])
        return move, o2

    def test_an_overlay_that_already_clears_is_left_alone(self):
        self.assertEqual(F.clear_of_webcam(self.overlay(), [], [span(YT, 0.0, 5.0)], self.canvas), {"dx": 0.0, "dy": 0.0})

    def test_no_webcam_no_move(self):
        self.assertEqual(F.clear_of_webcam(self.overlay(w=1800), [], [span(None, 0.0, 5.0)], self.canvas), {"dx": 0.0, "dy": 0.0})

    def test_a_wide_title_is_moved_left_off_a_top_right_webcam(self):
        o = self.overlay(w=1200)
        move, o2 = self.moved(o)
        self.assertLess(move["dx"], 0)
        self.assertEqual(move["dy"], 0.0)
        self.assertEqual(F.overlay_collisions([o2], [], [span(YT, 0.0, 5.0)], self.canvas), [])
        self.assertGreaterEqual(o2["x"] * 1920 - o2["w"] / 2, 0.015 * 1920 - 1e-6)           # still inside the frame

    def test_it_moves_as_little_as_it_can(self):
        o = self.overlay(w=1200)
        move, _ = self.moved(o)
        right_edge = 960 + 600
        webcam_left = YT["x"] * 1920 - 0.012 * 1920
        self.assertAlmostEqual(-move["dx"], right_edge - webcam_left, delta=1.0)

    def test_a_webcam_on_the_left_moves_it_right(self):
        o = self.overlay(w=1300, y=0.5)
        move, o2 = self.moved(o, spans=[span(KICK, 0.0, 5.0)])
        self.assertGreater(move["dx"], 0)
        self.assertEqual(F.overlay_collisions([o2], [], [span(KICK, 0.0, 5.0)], self.canvas), [])

    def test_when_it_cannot_go_sideways_it_goes_up_or_down(self):
        o = self.overlay(w=1850)                     # no room left of the webcam without leaving the frame
        move, o2 = self.moved(o)
        self.assertEqual(move["dx"], 0.0)
        self.assertNotEqual(move["dy"], 0.0)
        self.assertEqual(F.overlay_collisions([o2], [], [span(YT, 0.0, 5.0)], self.canvas), [])

    def test_the_zoomed_webcam_footprint_is_what_it_clears(self):
        o = self.overlay(w=1300)
        keys = [key(0.0, 1.5, 0.5, 0.5, "linear"), key(5.0, 1.5, 0.5, 0.5, "linear")]
        _, o2 = self.moved(o, keys=keys)
        self.assertEqual(F.overlay_collisions([o2], keys, [span(YT, 0.0, 5.0)], self.canvas), [])

    def test_when_nothing_fits_it_says_so(self):
        o = self.overlay(w=1850, h=1000, y=0.5)
        self.assertIsNone(F.clear_of_webcam(o, [], [span(YT, 0.0, 5.0)], self.canvas))


if __name__ == "__main__":
    unittest.main()
