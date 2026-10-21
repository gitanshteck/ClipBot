"""Tests for clipbot/editspec.py: the edit spec's validation, defaults and round trip."""

import json
import tempfile
import unittest
from pathlib import Path

from clipbot import editspec as E

SOURCES = {"v": {"file": "D:/x.mp4"}, "w": {"file": "D:/y.mp4"}}


def spec(clips, **extra):
    d = {"name": "t", "sources": SOURCES, "clips": clips}
    d.update(extra)
    return d


def clip(**kw):
    c = {"id": "a", "src": "v", "in": 10, "out": 20}
    c.update(kw)
    return c


class TestDefaults(unittest.TestCase):
    def test_minimal_spec_gets_every_default(self):
        n = E.normalize(spec([clip()]))
        self.assertEqual(n["profile"], "punchy")
        self.assertEqual((n["output"]["width"], n["output"]["height"], n["output"]["fps"]), (1920, 1080, 60))
        c = n["clips"][0]
        self.assertEqual(c["type"], "clip")
        self.assertEqual(c["transition_in"], {"type": "cut", "dur": 0.0})
        self.assertEqual(c["audio"]["fade_ms"], 30.0)
        self.assertEqual(n["master"]["loudness_lufs"], -14.0)
        self.assertEqual(n["master"]["true_peak_db"], -1.0)

    def test_output_preset_and_override(self):
        n = E.normalize(spec([clip()], output={"preset": "shorts_1080x1920", "fps": 30}))
        self.assertEqual((n["output"]["width"], n["output"]["height"], n["output"]["fps"]), (1080, 1920, 30))
        self.assertEqual(n["output"]["fit"], "blur")

    def test_dimensions_are_made_even(self):
        n = E.normalize(spec([clip()], output={"width": 1281, "height": 721}))
        self.assertEqual((n["output"]["width"], n["output"]["height"]), (1280, 720))

    def test_fx_ids_are_assigned(self):
        n = E.normalize(spec([clip(fx=[{"type": "flash", "at": 12, "dur": 0.1}])]))
        self.assertEqual(n["clips"][0]["fx"][0]["id"], "fx1")

    def test_a_card_needs_no_source(self):
        n = E.normalize(spec([clip(), {"id": "c", "type": "card", "dur": 2.0}]))
        self.assertEqual(n["clips"][1]["type"], "card")


class TestRoundTrip(unittest.TestCase):
    def test_normalised_spec_loads_back_unchanged(self):
        n = E.normalize(spec([
            clip(speed=[{"at": 12, "rate": 1.0}, {"at": 13, "rate": 0.5}, {"at": 15, "rate": 1.0}],
                 camera=[{"at": 12, "zoom": 1.0}, {"at": 13, "zoom": 1.2, "ease": "out2"}],
                 markers=[{"at": 13, "name": "kill"}],
                 overlays=[{"kind": "callout", "at": 13, "dur": 2.0, "text": "HEADSHOT"}]),
            {"id": "b", "src": "w", "in": 0, "out": 5, "transition_in": {"type": "xfade", "dur": 0.4}},
        ]))
        again = E.normalize(json.loads(E.to_json(n)))
        self.assertEqual(again, n)

    def test_load_reads_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(spec([clip()])), encoding="utf-8")
            self.assertEqual(E.load(p)["clips"][0]["id"], "a")

    def test_load_reports_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(E.SpecError):
                E.load(p)


class TestRejections(unittest.TestCase):
    def assertRejects(self, raw, fragment):
        with self.assertRaises(E.SpecError) as cm:
            E.normalize(raw)
        self.assertIn(fragment, str(cm.exception))

    def test_every_message_names_its_field(self):
        self.assertRejects(spec([clip(src="zz")]), "spec.clips[0].src")

    def test_empty_edit(self):
        self.assertRejects(spec([]), "at least one clip")

    def test_bad_name(self):
        self.assertRejects(dict(spec([clip()]), name="a b!"), "spec.name")

    def test_relative_source_path(self):
        self.assertRejects(spec([clip()], sources={"v": {"file": "x.mp4"}}), "absolute path")

    def test_out_before_in(self):
        self.assertRejects(spec([clip(**{"in": 5, "out": 1})]), "out must be at least")

    def test_unknown_key_lists_the_allowed_ones(self):
        self.assertRejects(spec([clip(bogus=1)]), "allowed:")

    def test_duplicate_ids(self):
        self.assertRejects(spec([clip(), clip()]), "duplicate clip id")

    def test_speed_outside_range(self):
        self.assertRejects(spec([clip(speed=[{"at": 12, "rate": 9}])]), "must be <= 4.0")

    def test_unknown_ease(self):
        self.assertRejects(spec([clip(camera=[{"at": 12, "zoom": 1.0, "ease": "bounce"}])]), "ease")

    def test_zoom_below_one(self):
        self.assertRejects(spec([clip(camera=[{"at": 12, "zoom": 0.5}])]), "zoom")

    def test_sfx_anchored_to_a_missing_clip(self):
        raw = spec([clip()], tracks={"sfx": [{"id": "s", "asset": "sfx_08a073fd8c00c828", "on": {"clip": "nope", "at": 12}}]})
        self.assertRejects(raw, "nope")

    def test_unknown_color(self):
        self.assertRejects(spec([clip(overlays=[{"kind": "text", "at": 12, "dur": 2, "text": "x", "color": "red"}])]), "color")


class TestEasing(unittest.TestCase):
    def test_endpoints_and_midpoints(self):
        for kind in E.EASES:
            self.assertAlmostEqual(E.ease_value(kind, 0.0), 0.0)
            self.assertAlmostEqual(E.ease_value(kind, 1.0), 1.0)
        self.assertAlmostEqual(E.ease_value("linear", 0.3), 0.3)
        self.assertAlmostEqual(E.ease_value("inout2", 0.5), 0.5)
        self.assertGreater(E.ease_value("out2", 0.5), 0.5)   # arrives fast
        self.assertLess(E.ease_value("in2", 0.5), 0.5)       # leaves slowly

    def test_progress_is_clamped(self):
        self.assertEqual(E.ease_value("out3", -1.0), 0.0)
        self.assertEqual(E.ease_value("out3", 2.0), 1.0)

    def test_unknown_kind(self):
        with self.assertRaises(E.SpecError):
            E.ease_value("bounce", 0.5)


class TestProfiles(unittest.TestCase):
    def test_profiles_get_gentler(self):
        p, b, r = (E.PROFILES[k] for k in ("punchy", "balanced", "restrained"))
        self.assertLess(p["max_gap"], b["max_gap"])
        self.assertLess(b["max_gap"], r["max_gap"])
        self.assertLess(p["slowmo_rate"], r["slowmo_rate"])
        self.assertEqual(p["transition"], 0.0)

    def test_every_output_preset_is_even_and_complete(self):
        for name, o in E.OUTPUT_PRESETS.items():
            self.assertEqual(o["width"] % 2, 0, name)
            self.assertEqual(o["height"] % 2, 0, name)
            for key in ("fps", "crf", "x264_preset", "audio_bitrate", "sample_rate", "gop_seconds", "bframes"):
                self.assertIn(key, o, name)


if __name__ == "__main__":
    unittest.main()
