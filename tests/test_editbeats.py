"""editbeats.py: the named moves and transition presets - as fragments that the spec accepts and the timeline times."""

import shutil
import subprocess
import unittest

from clipbot import editbeats as B
from clipbot import editspec as E
from clipbot import edittemplates as TPL
from clipbot import edittimeline as T
from clipbot.specerror import SpecError

SFX = {"hit": {"asset": "sfx_08a073fd8c00c828", "gain_db": -3}, "whoosh": {"asset": "sfx_1a98e97b627abc75"},
       "glitch": {"asset": "sfx_6078e14ca1ff7fec"}}


def ctx(lo=100.0, hi=112.0, sfx=SFX, **kw):
    return B.Ctx("c1", "v", lo, hi, sfx=sfx, stickers={"skull": "stk_baa8e3d5bb929faf"}, **kw)


def spec_with(clip_extra, sfx_items=(), extra_clips=()):
    clip = {"id": "c1", "src": "v", "in": 100.0, "out": 112.0}
    clip.update(clip_extra)
    return E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [clip] + list(extra_clips),
                        "tracks": {"music": [], "sfx": list(sfx_items)}})


def apply(frag, host=None):
    """The clip a fragment makes when merged into a bare one."""
    parts = {}
    B.merge(parts, frag)
    c = dict(host or {})
    if parts.get("freezes"):
        c["freezes"] = parts["freezes"]
    for k in ("fx", "overlays", "markers"):
        c[k] = list(c.get(k, [])) + parts.get(k, [])
    return c, parts


class TestFreezeZoom(unittest.TestCase):
    def test_it_holds_the_frame_punches_in_flashes_and_hits(self):
        f = B.expand({"do": "freeze_zoom", "at": 105.0}, ctx())
        self.assertEqual(f["freezes"], [{"at": 105.0, "dur": 0.7}])
        self.assertEqual([x["type"] for x in f["fx"]], ["flash"])
        self.assertEqual(f["punches"], [{"at": 105.0, "zoom": 1.22}])        # a camera zoom, not an fx punch: safe framing keeps the webcam in
        self.assertEqual(f["sfx"][0]["on"], {"clip": "c1", "at": 105.0})
        self.assertEqual(f["sfx"][0]["asset"], SFX["hit"]["asset"])

    def test_the_zoom_is_never_an_fx_punch_which_would_crop_a_corner_webcam(self):
        for beat in ({"do": "freeze_zoom", "at": 105.0}, {"do": "impact", "at": 105.0}, {"do": "caption_pop", "at": 105.0, "text": "x"}):
            self.assertNotIn("punch", [x["type"] for x in B.expand(beat, ctx())["fx"]], beat)

    def test_the_clip_is_exactly_the_hold_longer(self):
        c, parts = apply(B.expand({"do": "freeze_zoom", "at": 105.0, "hold": 0.9}, ctx()))
        spec = spec_with(c, parts["sfx"])
        plain = T.compile_timeline(spec_with({}))["duration"]
        self.assertAlmostEqual(T.compile_timeline(spec)["duration"] - plain, 0.9, delta=0.04)        # whole frames

    def test_text_is_an_overlay_for_the_hold_and_a_moment_after(self):
        f = B.expand({"do": "freeze_zoom", "at": 105.0, "text": "wait what", "text_y": 0.7}, ctx(brand={"title_style": "sty_1"}))
        o = f["overlays"][0]
        self.assertEqual((o["text"], o["y"], o["style"], o["anim_in"]), ("WAIT WHAT", 0.7, "sty_1", "pop"))
        self.assertGreater(o["dur"], 0.7)

    def test_no_flash_and_no_sound_when_asked_not_to(self):
        f = B.expand({"do": "freeze_zoom", "at": 105.0, "flash": 0, "sfx": None}, ctx())
        self.assertEqual(f["fx"], [])
        self.assertEqual(f["sfx"], [])


class TestImpactAndCaption(unittest.TestCase):
    def test_impact_is_a_shake_a_flash_a_sound_and_optionally_a_sticker(self):
        f = B.expand({"do": "impact", "at": 105.0, "sticker": "skull"}, ctx())
        self.assertEqual([x["type"] for x in f["fx"]], ["shake", "flash", "sticker"])
        self.assertEqual(f["fx"][2]["asset"], "stk_baa8e3d5bb929faf")                   # a role resolves to its asset
        self.assertEqual(B.expand({"do": "impact", "at": 105.0, "sticker": "stk_aaaaaaaaaaaaaaaa"}, ctx())["fx"][2]["asset"], "stk_aaaaaaaaaaaaaaaa")
        c, parts = apply(f)
        spec_with(c, parts["sfx"])                                                        # the spec accepts it

    def test_caption_pop_upper_cases_pops_and_keeps_to_the_clip(self):
        f = B.expand({"do": "caption_pop", "at": 105.0, "text": "bro thought he was safe"}, ctx())
        o = f["overlays"][0]
        self.assertEqual((o["text"], o["anim_in"], o["size"]), ("BRO THOUGHT HE WAS SAFE", "pop", 0.08))
        c, parts = apply(f)
        spec_with(c, parts["sfx"])

    def test_caption_pop_needs_its_text(self):
        with self.assertRaises(SpecError):
            B.expand({"do": "caption_pop", "at": 105.0, "text": "  "}, ctx())


class TestReplay(unittest.TestCase):
    def test_a_slower_copy_of_the_moment_follows_the_clip(self):
        f = B.expand({"do": "replay", "at": 105.0}, ctx())
        r = f["after"][0]
        self.assertEqual((r["id"], r["src"], r["in"], r["out"], r["speed"]), ("c1r1", "v", 102.8, 106.0, 0.5))
        self.assertEqual(r["transition_in"], {"type": "flash", "dur": 0.15})
        self.assertEqual(r["overlays"][0]["text"], "REPLAY")
        self.assertEqual(f["sfx"][0]["on"]["clip"], "c1r1")                               # the whoosh belongs to the replay, not the host
        self.assertGreaterEqual(len(r["camera"]), 2)                                      # events for the pacing gate

    def test_its_length_is_the_range_over_the_rate_and_the_spec_takes_it(self):
        f = B.expand({"do": "replay", "at": 105.0, "back": 2.0, "forward": 1.0, "rate": 0.5}, ctx())
        spec = spec_with({}, f["sfx"], f["after"])
        clips = T.compile_timeline(spec)["clips"]
        self.assertEqual([c["id"] for c in clips], ["c1", "c1r1"])
        self.assertAlmostEqual(clips[1]["dur"], 6.0, delta=0.05)

    def test_the_replay_keeps_the_hosts_grade_and_gain(self):
        f = B.expand({"do": "replay", "at": 105.0}, ctx(grade={"preset": "neutral_punch", "strength": "subtle"}, gain_db=2.0))
        self.assertEqual(f["after"][0]["grade"], {"preset": "neutral_punch", "strength": "subtle"})
        self.assertEqual(f["after"][0]["audio"]["gain_db"], 2.0)

    def test_a_long_replay_has_camera_keys_often_enough_for_the_pacing_gate(self):
        f = B.expand({"do": "replay", "at": 105.0, "back": 2.4, "forward": 1.2}, ctx(max_gap=5.0))
        keys = [k["at"] for k in f["after"][0]["camera"]]
        self.assertEqual(keys, sorted(set(keys)))
        gaps_out = [(b - a) / 0.5 for a, b in zip(keys, keys[1:])]
        self.assertLessEqual(max(gaps_out), 5.0 * 0.8 + 1e-6)                           # output seconds between keys
        self.assertEqual((keys[0], keys[-1]), (102.6, 106.2))
        self.assertGreater(len(keys), 2)

    def test_a_second_replay_on_the_same_clip_gets_its_own_id(self):
        self.assertEqual(B.expand({"do": "replay", "at": 105.0}, ctx(n=1))["after"][0]["id"], "c1r2")

    def test_it_cannot_start_before_the_stream(self):
        f = B.expand({"do": "replay", "at": 0.5, "back": 2.0}, B.Ctx("c1", "v", 0.0, 12.0, sfx=SFX))
        self.assertEqual(f["after"][0]["in"], 0.0)


class TestErrors(unittest.TestCase):
    def test_a_beat_is_never_silently_dropped(self):
        bad = ({"do": "nope", "at": 105}, {"at": 105}, "freeze_zoom", {"do": "freeze_zoom"}, {"do": "freeze_zoom", "at": 105, "hold": 9},
               {"do": "freeze_zoom", "at": 105, "zoom": 3}, {"do": "freeze_zoom", "at": "soon"}, {"do": "impact", "at": 105, "colour": "red"},
               {"do": "freeze_zoom", "at": 100.1}, {"do": "freeze_zoom", "at": 111.9}, {"do": "freeze_zoom", "at": 105, "sfx": "boing"})
        for b in bad:
            with self.assertRaises(SpecError, msg=str(b)):
                B.expand(b, ctx())

    def test_the_message_says_what_to_do(self):
        with self.assertRaises(SpecError) as c:
            B.expand({"do": "freeze_zoom", "at": 105, "hold": 9}, ctx())
        self.assertIn("`hold` must be between", str(c.exception))
        with self.assertRaises(SpecError) as c:
            B.expand({"do": "impact", "at": 105, "colour": "red"}, ctx())
        self.assertIn("unknown parameter(s) colour", str(c.exception))

    def test_a_role_with_no_sound_plays_none_and_says_so(self):
        f = B.expand({"do": "impact", "at": 105.0}, ctx(sfx={}))
        self.assertEqual(f["sfx"], [])
        self.assertIn("no `hit` sound", f["warnings"][0])


class TestTransitions(unittest.TestCase):
    def pair(self, prev_extra=None, nxt_extra=None):
        prev = {"id": "a", "src": "v", "in": 10.0, "out": 20.0, "camera": [{"at": 10.0, "zoom": 1.0, "ease": "inout2"}, {"at": 20.0, "zoom": 1.04, "ease": "inout2"}]}
        nxt = {"id": "b", "src": "v", "in": 50.0, "out": 60.0, "camera": [{"at": 50.0, "zoom": 1.0, "ease": "inout2"}, {"at": 60.0, "zoom": 1.04, "ease": "inout2"}]}
        prev.update(prev_extra or {})
        nxt.update(nxt_extra or {})
        return prev, nxt

    def test_whip_is_a_horizontal_blur_streak_with_a_whoosh_where_the_next_clip_starts(self):
        prev, nxt = self.pair()
        r = B.apply_transition("whip", prev, nxt, SFX)
        self.assertEqual(nxt["transition_in"], {"type": "xfade", "name": "hblur", "dur": 0.3})
        self.assertEqual(r["sfx"][0]["on"], {"clip": "b", "at": 50.0})
        self.assertEqual(r["sfx"][0]["asset"], SFX["whoosh"]["asset"])

    def test_glitch_is_a_pixelate_with_the_glitch_sound(self):
        prev, nxt = self.pair()
        r = B.apply_transition("glitch", prev, nxt, SFX)
        self.assertEqual((nxt["transition_in"]["name"], nxt["transition_in"]["dur"]), ("pixelize", 0.2))
        self.assertEqual(r["sfx"][0]["asset"], SFX["glitch"]["asset"])

    def test_zoom_through_pushes_both_clips_under_a_cross_fade(self):
        prev, nxt = self.pair()
        r = B.apply_transition("zoom_through", prev, nxt, SFX)
        self.assertEqual(nxt["transition_in"], {"type": "xfade", "name": "fade", "dur": 0.3})
        self.assertEqual(prev["camera"][-1], {"at": 20.0, "zoom": 1.35, "ease": "in2"})
        self.assertEqual(nxt["camera"][0], {"at": 50.0, "zoom": 1.35, "ease": "out2"})
        self.assertAlmostEqual(nxt["camera"][1]["at"], 50.4, places=2)
        self.assertEqual(r["warnings"], [])
        # the keys still rise, so the spec takes them
        for c in (prev, nxt):
            keys = [k["at"] for k in c["camera"]]
            self.assertEqual(keys, sorted(set(keys)))
        E.normalize({"name": "t", "sources": {"v": {"file": "D:/x.mp4"}}, "clips": [prev, nxt]})

    def test_zoom_through_falls_back_to_a_cross_fade_and_says_so_when_a_punch_is_in_the_way(self):
        prev, nxt = self.pair(prev_extra={"camera": [{"at": 18.5, "zoom": 1.3, "ease": "out2"}, {"at": 19.6, "zoom": 1.0, "ease": "inout2"}]})
        before = [dict(k) for k in prev["camera"]]
        r = B.apply_transition("zoom_through", prev, nxt, SFX)
        self.assertEqual(prev["camera"], before)
        self.assertEqual(nxt["transition_in"]["name"], "fade")
        self.assertIn("a punch is in the way", r["warnings"][0])

    def test_zoom_through_falls_back_when_a_card_is_on_either_side(self):
        card = {"id": "s", "type": "card", "dur": 3.0}
        _, nxt = self.pair()
        r = B.apply_transition("zoom_through", card, nxt, SFX)
        self.assertIn("a card has no picture", r["warnings"][0])

    def test_flash_dip_and_crossfade_presets(self):
        for name, want in (("flash", {"type": "flash", "dur": 0.15}), ("dip", {"type": "dip", "dur": 0.3}),
                           ("crossfade", {"type": "xfade", "name": "fade", "dur": 0.4})):
            prev, nxt = self.pair()
            B.apply_transition(name, prev, nxt, SFX)
            self.assertEqual(nxt["transition_in"], want, name)

    def test_the_first_clip_has_nothing_to_transition_from_and_an_unknown_name_is_refused(self):
        _, nxt = self.pair()
        r = B.apply_transition("whip", None, nxt, SFX)
        self.assertIn("nothing to transition from", r["warnings"][0])
        self.assertNotIn("transition_in", nxt)
        with self.assertRaises(SpecError):
            B.apply_transition("swoosh", None, nxt, SFX)

    def test_a_missing_sound_is_a_warning_not_a_failure(self):
        prev, nxt = self.pair()
        r = B.apply_transition("whip", prev, nxt, {})
        self.assertEqual(r["sfx"], [])
        self.assertIn("no `whoosh` sound", r["warnings"][0])
        self.assertEqual(nxt["transition_in"]["name"], "hblur")


class TestEpisodeIntegration(unittest.TestCase):
    def lineup(self, **chapter):
        ch = {"title": "Chapter Two", "clips": [{"src": "v", "in": 200.0, "out": 214.0, "beats": [{"do": "freeze_zoom", "at": 206.0, "text": "wait what"},
                                                                                               {"do": "replay", "at": 206.0}]}]}
        ch.update(chapter)
        return {"name": "t", "series": "S", "number": 1, "profile": "restrained", "sources": {"v": {"file": "D:/x.mp4"}},
                "beat_sfx": SFX, "sfx": {"chapter": {"asset": "sfx_1a98e97b627abc75"}},
                "chapters": [{"title": "Chapter One", "clips": [{"src": "v", "in": 100.0, "out": 112.0}]}, ch]}

    def test_beats_become_part_of_the_clip_and_a_replay_a_clip_after_it(self):
        facts = {}
        spec = E.normalize(TPL.episode(self.lineup(), facts))
        ids = [c["id"] for c in spec["clips"]]
        self.assertEqual(ids, ["c1_01", "c2_02", "c2_02r2"])               # the replay is the host's second beat
        host = next(c for c in spec["clips"] if c["id"] == "c2_02")
        self.assertEqual(len(host["freezes"]), 1)
        self.assertTrue(any(o["text"] == "WAIT WHAT" for o in host["overlays"]))
        self.assertTrue(any(c["id"].startswith("c2_02r") for c in spec["clips"]))
        self.assertEqual(facts["warnings"], [])
        T.compile_timeline(spec)

    def test_a_chapter_transition_is_set_on_its_first_clip_and_replaces_the_chapter_whoosh(self):
        spec = E.normalize(TPL.episode(self.lineup(transition="whip")))
        first = next(c for c in spec["clips"] if c["id"] == "c2_02")
        self.assertEqual((first["transition_in"]["type"], first["transition_in"]["name"]), ("xfade", "hblur"))
        names = [s["id"] for s in spec["tracks"]["sfx"]]
        self.assertFalse(any(n.startswith("wc2") for n in names), names)                 # no chapter whoosh on top of the whip's
        self.assertTrue(any(n.startswith("tc2") for n in names), names)

    def test_a_chapter_without_a_transition_keeps_its_whoosh_and_its_hard_cut(self):
        spec = E.normalize(TPL.episode(self.lineup()))
        first = next(c for c in spec["clips"] if c["id"] == "c2_02")
        self.assertEqual(first["transition_in"]["type"], "cut")
        self.assertTrue(any(s["id"].startswith("wc2") for s in spec["tracks"]["sfx"]))

    def test_warnings_reach_the_facts(self):
        lu = self.lineup(transition="whip")
        lu["beat_sfx"] = {}
        facts = {}
        TPL.episode(lu, facts)
        self.assertTrue(any("no `whoosh` sound" in w for w in facts["warnings"]), facts["warnings"])

    def test_a_bad_beat_is_a_spec_error_naming_it(self):
        lu = self.lineup()
        lu["chapters"][1]["clips"][0]["beats"] = [{"do": "freeze_zoom", "at": 999.0}]
        with self.assertRaises(SpecError):
            TPL.episode(lu)

    def test_a_freeze_zoom_becomes_camera_keys_on_the_clip_and_keeps_the_keys_rising(self):
        spec = E.normalize(TPL.episode(self.lineup()))
        host = next(c for c in spec["clips"] if c["id"] == "c2_02")
        zooms = [k for k in host["camera"] if k["zoom"] >= 1.2]
        self.assertTrue(zooms, host["camera"])
        keys = [k["at"] for k in host["camera"]]
        self.assertEqual(keys, sorted(set(keys)))
        self.assertFalse(any(f["type"] == "punch" for f in host["fx"]))

    def test_on_a_kill_clip_the_freeze_zoom_rides_the_kill_camera_too(self):
        lu = self.lineup()
        kill = {"t": 150.0, "weapon": "gun", "dist_m": 40.0, "headshot": False, "source": "feed", "id": "k1"}
        lu["chapters"][1]["clips"][0].update({"in": 140.0, "out": 160.0, "kills": [kill], "beats": [{"do": "freeze_zoom", "at": 155.0}]})
        host = next(c for c in E.normalize(TPL.episode(lu))["clips"] if c["id"] == "c2_02")
        self.assertTrue(any(abs(k["at"] - 155.0) < 0.01 and k["zoom"] >= 1.2 for k in host["camera"]), host["camera"])

    def test_a_lineup_without_beats_builds_exactly_what_it_did_before(self):
        lu = self.lineup()
        lu["chapters"][1]["clips"][0].pop("beats")
        spec = E.normalize(TPL.episode(lu))
        self.assertEqual([c["id"] for c in spec["clips"]], ["c1_01", "c2_02"])
        self.assertEqual(spec["clips"][1]["freezes"], [])


class TestXfadeNames(unittest.TestCase):
    def test_every_name_the_spec_accepts_is_a_transition_the_vendored_ffmpeg_has(self):
        try:
            from clipbot.config import load_settings
            from clipbot.utils import resolve_tool
            ffmpeg = resolve_tool(load_settings().tool("ffmpeg"), "")
        except Exception:  # noqa: BLE001
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is not available")
        help_text = subprocess.run([ffmpeg, "-hide_banner", "-h", "filter=xfade"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   universal_newlines=True).stdout
        have = {line.split()[0] for line in help_text.splitlines() if line.startswith("     ") and len(line.split()) > 2 and line.split()[1].lstrip("-").isdigit()}
        self.assertIn("hblur", have)
        missing = sorted(set(E.XFADE_NAMES) - have)
        self.assertEqual(missing, [], "the spec accepts transitions this ffmpeg does not have")
        self.assertIn("hblur", E.XFADE_NAMES)


if __name__ == "__main__":
    unittest.main()
