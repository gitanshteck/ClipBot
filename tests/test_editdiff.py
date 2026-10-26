"""editdiff.py: what a changelog line says, and what it must not say."""

import unittest

from clipbot import editdiff as D


def clip(cid, a=10.0, b=20.0, **extra):
    c = {"id": cid, "type": "clip", "label": cid.upper(), "src": "v", "in": a, "out": b, "speed": [], "freezes": [], "camera": [],
         "grade": {}, "fx": [], "markers": [], "overlays": [], "framing": None, "audio": {"gain_db": 0.0, "mute": False},
         "transition_in": {"type": "cut", "dur": 0.0}}
    c.update(extra)
    return c


def spec(*clips, **extra):
    s = {"profile": "restrained", "output": {"width": 1920, "height": 1080, "fps": 60}, "master": {"loudness_lufs": -14.0, "glue": True},
         "captions": None, "tracks": {"music": [], "sfx": []}, "clips": list(clips)}
    s.update(extra)
    return s


class TestClips(unittest.TestCase):
    def test_a_first_render_has_nothing_to_compare(self):
        d = D.diff_specs(None, spec(clip("a"), clip("b")))
        self.assertTrue(d["first"])
        self.assertEqual(d["lines"], ["first render: 2 clip(s)"])
        self.assertEqual(D.summary(d), "first render: 2 clip(s)")

    def test_identical_specs_say_so(self):
        d = D.diff_specs(spec(clip("a")), spec(clip("a")))
        self.assertEqual(d["lines"], ["no change to the spec (a re-render)"])
        self.assertEqual(D.summary(d), "no spec change")

    def test_clips_added_and_removed_are_named_with_their_labels(self):
        d = D.diff_specs(spec(clip("a"), clip("b")), spec(clip("a"), clip("c", label="new one")))
        self.assertEqual((d["added"], d["removed"]), (["c"], ["b"]))
        self.assertIn("added c (new one)", d["lines"])
        self.assertIn("removed b (B)", d["lines"])
        self.assertEqual(D.summary(d), "+1 -1 clips")

    def test_a_trim_says_which_edge_moved_and_how_much_the_clip_grew(self):
        d = D.diff_specs(spec(clip("a", 10, 20)), spec(clip("a", 9.5, 20)))
        self.assertEqual(d["changed"]["a"], ["in 10 -> 9.5 s, clip 0.5 s longer"])
        d = D.diff_specs(spec(clip("a", 10, 20)), spec(clip("a", 10, 18.25)))
        self.assertEqual(d["changed"]["a"], ["out 20 -> 18.25 s, clip 1.75 s shorter"])

    def test_a_clip_slid_without_changing_length_is_moved_not_trimmed(self):
        d = D.diff_specs(spec(clip("a", 10, 20)), spec(clip("a", 12, 22)))
        self.assertEqual(d["changed"]["a"], ["in 10 -> 12 s, out 20 -> 22 s, moved"])

    def test_rounding_is_not_an_edit(self):
        d = D.diff_specs(spec(clip("a", 10.0, 20.0)), spec(clip("a", 10.01, 20.015)))
        self.assertEqual(d["changed"], {})

    def test_gain_captions_label_transition_and_overlay_text(self):
        old = clip("a", overlays=[{"kind": "text", "text": "OLD"}])
        new = clip("a", overlays=[{"kind": "text", "text": "NEW"}], captions=True, label="better label",
                   audio={"gain_db": -3.0, "mute": False}, transition_in={"type": "xfade", "dur": 0.4})
        lines = D.diff_specs(spec(old), spec(new))["changed"]["a"]
        self.assertIn("gain 0 -> -3 dB", lines)
        self.assertIn("captions default -> on", lines)
        self.assertIn("transition in: cut -> xfade", lines)
        self.assertIn("text 'OLD' -> 'NEW'", lines)
        self.assertTrue(any(l.startswith("label ") for l in lines))

    def test_camera_effects_and_markers_are_counted_not_dumped(self):
        old = clip("a", camera=[{"t": 0, "zoom": 1.0}])
        new = clip("a", camera=[{"t": 0, "zoom": 1.0}, {"t": 1, "zoom": 1.2}], fx=[{"type": "punch"}], markers=[{"at": 1, "name": "kill"}])
        lines = D.diff_specs(spec(old), spec(new))["changed"]["a"]
        self.assertIn("camera moves changed (1 -> 2 keys)", lines)
        self.assertIn("effects changed (0 -> 1)", lines)
        self.assertIn("markers changed (0 -> 1)", lines)

    def test_a_card_changes_in_length_and_colour(self):
        old = {"id": "s", "type": "card", "label": "Intro", "dur": 2.6, "bg": "#0B0B0F", "overlays": [], "audio": {"gain_db": 0.0}}
        new = dict(old, dur=3.0, bg="#000000")
        self.assertEqual(D.diff_specs(spec(old), spec(new))["changed"]["s"], ["card length 2.6 -> 3 s", "card colour #0B0B0F -> #000000"])


class TestOrder(unittest.TestCase):
    def test_moving_one_clip_is_one_moved_clip_however_far_it_went(self):
        a = spec(*[clip(i) for i in "abcdef"])
        b = spec(*[clip(i) for i in "fabcde"])
        d = D.diff_specs(a, b)
        self.assertEqual(d["moved"], ["f"])
        self.assertIn("reordered: 1 clip(s) moved (f)", d["lines"])

    def test_the_same_order_is_not_a_reorder_even_when_clips_come_and_go(self):
        d = D.diff_specs(spec(*[clip(i) for i in "abcd"]), spec(*[clip(i) for i in "acxd"]))
        self.assertEqual(d["moved"], [])

    def test_the_move_helper(self):
        self.assertEqual(D._moved(list("abcde"), list("abcde")), [])
        self.assertEqual(D._moved(list("abcde"), list("acdbe")), ["b"])
        self.assertEqual(D._moved([], []), [])


class TestGlobals(unittest.TestCase):
    def test_profile_output_master_and_captions_block(self):
        old = spec(clip("a"))
        new = spec(clip("a"), profile="punchy", output={"width": 1280, "height": 720, "fps": 30},
                   master={"loudness_lufs": -16.0, "glue": True}, captions={"size": 0.06, "highlight": "#fff"})
        g = D.diff_specs(old, new)["global"]
        self.assertIn("profile restrained -> punchy", g)
        self.assertIn("output 1920x1080@60 -> 1280x720@30", g)
        self.assertIn("master loudness_lufs: -14.0 -> -16.0", g)
        self.assertIn("captions turned on", g)
        g2 = D.diff_specs(new, spec(clip("a"), profile="punchy", output=new["output"], master=new["master"],
                                    captions={"size": 0.07, "highlight": "#fff"}))["global"]
        self.assertEqual(g2, ["captions size: 0.06 -> 0.07"])

    def test_music_and_sfx_tracks_added_removed_and_changed(self):
        old = spec(clip("a"), tracks={"music": [{"id": "bed", "gain_db": -12}], "sfx": [{"id": "s1"}]})
        new = spec(clip("a"), tracks={"music": [{"id": "bed", "gain_db": -18}, {"id": "bed2"}], "sfx": []})
        g = D.diff_specs(old, new)["global"]
        self.assertIn("music added: bed2", g)
        self.assertIn("music changed: bed", g)
        self.assertIn("sfx removed: s1", g)

    def test_a_long_list_is_capped_with_a_count_of_the_rest(self):
        old = spec(*[clip("c{0}".format(i)) for i in range(100)])
        new = spec(*[clip("c{0}".format(i), 10, 25) for i in range(100)])
        lines = D.diff_specs(old, new)["lines"]
        self.assertEqual(len(lines), D.MAX_LINES + 1)
        self.assertEqual(lines[-1], "... and {0} more".format(100 - D.MAX_LINES))


if __name__ == "__main__":
    unittest.main()
