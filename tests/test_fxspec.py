"""Validation, time rebasing and filter-string shape for clip effects.

The graph strings themselves are verified against real ffmpeg by rendering, not
here; what these tests pin is the stuff that is easy to break silently - the
absolute-time contract, the escaping rules, and the two ffmpeg keywords whose
absence produces a plausible-looking but wrong render (`normalize=0` on amix,
`eval=frame` on eq).

Run: python -m unittest discover -s tests
"""

import unittest

from clipbot import fxspec as F
from clipbot.specerror import SpecError

CANVAS = (1080, 1920)

ASSETS = {
    "sfx_00000000aaaa": {"path": r"D:\lib\airhorn.mp3", "duration": 1.42,
                         "name": "airhorn"},
    "mus_11111111bbbb": {"path": r"D:\lib\bed.mp3", "duration": 95.0, "name": "bed"},
    "stk_22222222cccc": {"path": r"D:\lib\pog.png", "width": 512, "height": 256,
                         "name": "pog", "animated": False},
}


def eff(**kw):
    kw.setdefault("id", "e1")
    return kw


class TestValidation(unittest.TestCase):
    def test_empty_is_empty(self):
        self.assertEqual(F.normalize_fx(None), [])
        self.assertEqual(F.normalize_fx([]), [])

    def test_unknown_type_rejected(self):
        with self.assertRaises(SpecError):
            F.normalize_fx([eff(type="glitch", at=1.0)])

    def test_out_of_range_rejected(self):
        with self.assertRaises(SpecError):
            F.normalize_fx([eff(type="punch", at=1.0, amount=9.0)])
        with self.assertRaises(SpecError):
            # atempo cannot do this in one instance, so we must not pretend to
            F.normalize_fx([eff(type="speed", rate=4.0)])

    def test_singletons(self):
        with self.assertRaises(SpecError):
            F.normalize_fx([eff(id="a", type="speed", rate=1.2),
                            eff(id="b", type="speed", rate=0.8)])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(SpecError):
            F.normalize_fx([eff(id="a", type="flash", at=1.0),
                            eff(id="a", type="flash", at=2.0)])

    def test_apostrophe_in_text_rejected_with_a_useful_message(self):
        with self.assertRaises(SpecError) as ctx:
            F.normalize_fx([eff(type="text", at=1.0, text="don't")])
        self.assertIn("’", str(ctx.exception))
        # The typographic one is fine.
        self.assertTrue(F.normalize_fx([eff(type="text", at=1.0, text="don’t")]))

    def test_asset_id_shape_enforced(self):
        with self.assertRaises(SpecError):
            F.normalize_fx([eff(type="sfx", at=1.0, asset="../../etc/passwd")])

    def test_disabled_effects_survive_validation(self):
        out = F.normalize_fx([eff(type="flash", at=1.0, enabled=False)])
        self.assertEqual(out[0]["enabled"], False)


class TestEscaping(unittest.TestCase):
    def test_windows_font_path(self):
        self.assertEqual(
            F.escape_path(r"D:\Claude\work\_library\fonts\Anton.ttf"),
            "'D\\:/Claude/work/_library/fonts/Anton.ttf'",
        )

    def test_font_path_with_quote_rejected(self):
        with self.assertRaises(SpecError):
            F.escape_path("D:/fonts/Bob's.ttf")

    def test_text_specials_escaped(self):
        self.assertEqual(F.escape_text("100%: a\\b"), r"100\%\: a\\b")


class TestResolution(unittest.TestCase):
    def _resolve(self, fx, origin=100.0, span=10.0, **kw):
        kw.setdefault("assets", ASSETS)
        kw.setdefault("fallback_font", "C:/Windows/Fonts/segoeuib.ttf")
        return F.resolve_fx(fx, origin, span, CANVAS, **kw)

    def test_absolute_time_is_rebased(self):
        plan = self._resolve([eff(type="flash", at=104.5, dur=0.2)])
        self.assertEqual(plan["video"][0]["start"], 4.5)

    def test_nudging_the_in_point_does_not_move_the_effect(self):
        # The whole reason `at` is absolute: the same effect against a clip
        # whose start moved 2s earlier must land at the same moment of stream.
        fx = [eff(type="flash", at=104.5, dur=0.2)]
        a = self._resolve(fx, origin=100.0, span=10.0)["video"][0]
        b = self._resolve(fx, origin=98.0, span=12.0)["video"][0]
        self.assertEqual(a["start"] + 100.0, b["start"] + 98.0)

    def test_effect_outside_the_clip_warns_rather_than_raises(self):
        plan = self._resolve([eff(type="flash", at=500.0, dur=0.2)])
        self.assertIsNone(plan)  # nothing survived

        plan = self._resolve([eff(id="a", type="flash", at=104.0, dur=0.2),
                              eff(id="b", type="flash", at=500.0, dur=0.2)])
        self.assertEqual(len(plan["video"]), 1)
        self.assertTrue(any("outside" in w for w in plan["warnings"]))

    def test_missing_asset_is_fatal_when_strict_and_a_warning_otherwise(self):
        fx = [eff(type="sfx", at=104.0, asset="sfx_deadbeefdead")]
        with self.assertRaises(SpecError):
            self._resolve(fx, strict=True)
        plan = self._resolve(fx, strict=False)
        self.assertIsNone(plan)

    def test_output_duration_accounts_for_hold_and_speed(self):
        plan = self._resolve([eff(id="f", type="freeze", dur=0.8),
                              eff(id="s", type="speed", rate=1.25)])
        self.assertAlmostEqual(plan["out_duration"], (10.0 + 0.8) / 1.25, places=3)

    def test_shake_amplitude_scales_with_canvas(self):
        fx = [eff(type="shake", at=104.0, dur=0.5, amount=8)]
        full = F.resolve_fx(fx, 100.0, 10.0, (1080, 1920), assets=ASSETS)
        prox = F.resolve_fx(fx, 100.0, 10.0, (360, 640), assets=ASSETS)
        # Same proportion of the frame, so the proxy preview shakes like the
        # real render rather than three times as hard.
        self.assertAlmostEqual(full["video"][0]["amount_px"] / 1080.0,
                               prox["video"][0]["amount_px"] / 360.0, places=6)

    def test_sticker_keeps_its_aspect_and_stays_in_frame(self):
        plan = self._resolve([eff(type="sticker", at=104.0, dur=1.0,
                                  asset="stk_22222222cccc", x=0.95, y=0.5, w=0.30)])
        s = plan["video"][0]
        self.assertEqual(s["h"], s["w"] // 2)          # source is 512x256
        self.assertLessEqual(s["x"] + s["w"], 1080)    # clamped inside the canvas

    def test_input_indices_match_input_order(self):
        plan = self._resolve([
            eff(id="s", type="sfx", at=104.0, asset="sfx_00000000aaaa"),
            eff(id="m", type="music", asset="mus_11111111bbbb"),
            eff(id="k", type="sticker", at=104.0, dur=1.0, asset="stk_22222222cccc"),
        ], next_input=2)
        # argv appends inputs in list order, so index N must be the Nth extra -i
        # or every asset ends up wired to the wrong filter.
        self.assertEqual([i["index"] for i in plan["inputs"]], [2, 3, 4])


class TestFilterShape(unittest.TestCase):
    def _plan(self, fx, **kw):
        kw.setdefault("assets", ASSETS)
        return F.resolve_fx(fx, 100.0, 10.0, CANVAS, **kw)

    def test_every_expression_is_single_quoted(self):
        plan = self._plan([eff(id="p", type="punch", at=104.0, dur=0.4),
                           eff(id="s", type="shake", at=104.0, dur=0.6),
                           eff(id="f", type="flash", at=104.0, dur=0.1)])
        chains = []
        chains += F.build_camera_chains(plan, "in", 1080, 1920)[0]
        chains += F.build_tail_chains(plan, "in")[0]
        graph = ";".join(chains)
        # An unquoted between(t,A,B) splits the filter at its first comma and
        # yields a parse error that points at the wrong filter entirely.
        for token in ("between(t,", "if(between"):
            for pos in _positions(graph, token):
                self.assertTrue(_inside_quotes(graph, pos),
                                "unquoted expression at offset {0}".format(pos))

    def test_eq_flash_uses_eval_frame(self):
        plan = self._plan([eff(type="flash", at=104.0, dur=0.1)])
        graph = ";".join(F.build_tail_chains(plan, "in")[0])
        # Without eval=frame the expression is evaluated once at init and the
        # flash simply never fires.
        self.assertIn("eval=frame", graph)

    def test_amix_never_normalises(self):
        plan = self._plan([eff(id="s", type="sfx", at=104.0,
                               asset="sfx_00000000aaaa"),
                           eff(id="m", type="music", asset="mus_11111111bbbb")])
        chains, label = F.build_audio_chains(plan, has_source_audio=True)
        graph = ";".join(chains)
        self.assertIn("amix=", graph)
        # amix divides by the input count by default, which would silently drop
        # the stream audio ~10 dB the moment a bed is added.
        self.assertIn("normalize=0", graph)
        self.assertEqual(label, "[a]")

    def test_no_audio_effects_leaves_the_audio_path_alone(self):
        plan = self._plan([eff(type="flash", at=104.0, dur=0.1)])
        chains, label = F.build_audio_chains(plan, has_source_audio=True)
        self.assertEqual(chains, [])
        self.assertIsNone(label)

    def test_audioless_source_never_references_stream_zero(self):
        plan = self._plan([eff(type="sfx", at=104.0, asset="sfx_00000000aaaa")])
        graph = ";".join(F.build_audio_chains(plan, has_source_audio=False)[0])
        # `-map 0:a:0?` tolerates a missing audio stream; `[0:a]` in a filter
        # graph is a hard error.
        self.assertNotIn("[0:a]", graph)


def _positions(text, token):
    start = 0
    while True:
        i = text.find(token, start)
        if i < 0:
            return
        yield i
        start = i + 1


def _inside_quotes(text, pos):
    return text.count("'", 0, pos) % 2 == 1


if __name__ == "__main__":
    unittest.main()
