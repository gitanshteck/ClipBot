"""The HUD look (AFTER ACTION): the overlay builders, the episode template in `look: "hud"`, and the synthesised cues."""

import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from clipbot import edithud as H
from clipbot import editmotion, editspec as E, edittemplates as TPL, edittimeline as T
from clipbot import hudsounds as S


def kill(t, dist=None, **kw):
    return dict({"id": "k%d" % int(t), "t": float(t), "dist_m": dist, "weapon": "gun", "headshot": False, "through_cover": False,
                 "victim": "", "icon": None}, **kw)


class TestContact(unittest.TestCase):
    def test_a_kill_with_a_range_carries_it_as_a_whole_number_and_lands_on_the_kill(self):
        o = H.contact(kill(100.0, 68.4, victim="Khanny", headshot=True), "HEADSHOT", "headshot", 3, 14)
        self.assertEqual((o["kind"], o["comp"], o["at"]), ("motion", "Contact", 100.0))
        p = o["props"]
        self.assertEqual((p["distance"], p["index"], p["total"], p["victim"], p["tone"]), (68, 3, 14, "Khanny", "head"))
        self.assertEqual(p["tags"], ["HEADSHOT"])
        self.assertEqual((o["dur"], o["hit"]), (H.CONTACT_DUR, H.CONTACT_HIT))

    def test_a_kill_the_feed_gave_no_range_for_shows_none_rather_than_a_guess(self):
        p = H.contact(kill(100.0), "KILL", "kill", 1, 2)["props"]
        self.assertNotIn("distance", p)
        self.assertEqual(p["headline"], "KILL CONFIRMED")

    def test_the_headline_never_repeats_what_the_plate_already_says(self):
        self.assertEqual(H.contact(kill(1, 68.0), "68 M", "long", 1, 1)["props"]["headline"], "KILL CONFIRMED")
        self.assertEqual(H.contact(kill(1, 11.0, headshot=True), "HEADSHOT", "headshot", 1, 1)["props"]["headline"], "KILL CONFIRMED")
        self.assertEqual(H.contact(kill(1, 11.0), "DOUBLE KILL", "multi", 1, 1)["props"]["headline"], "DOUBLE KILL")

    def test_a_victim_name_with_no_letters_is_ocr_noise_and_is_not_drawn(self):
        self.assertNotIn("victim", H.contact(kill(1, 49.0, victim="2134921691"), "KILL", "kill", 1, 1)["props"])
        self.assertEqual(H.contact(kill(1, 49.0, victim="Yutigi"), "KILL", "kill", 1, 1)["props"]["victim"], "Yutigi")

    def test_chips_and_icon_and_stack_row(self):
        k = kill(5.0, 25.0, weapon="grenade", through_cover=True, icon="D:\\x\\k.png")
        o = H.contact(k, "NADE KILL", "nade", 1, 1, y_px=444)
        self.assertEqual(o["props"]["tags"], ["NADE", "THROUGH COVER"])
        self.assertEqual(o["props"]["icon"], "file:D:/x/k.png")
        self.assertEqual(o["props"]["y"], 444)

    def test_the_hero_plate_is_bigger_longer_and_carries_its_note(self):
        o = H.contact(kill(5.0, 238.0), "KILL", "long", 14, 14, hero=True, note="LONGEST CONFIRMED")
        self.assertTrue(o["props"]["hero"])
        self.assertIn("LONGEST CONFIRMED", o["props"]["tags"])
        self.assertEqual((o["dur"], o["hit"]), (H.HERO_DUR, H.HERO_HIT))

    def test_the_changelog_names_a_graphic_by_what_it_shows(self):
        from clipbot import editdiff

        clip = {"overlays": [H.contact(kill(5.0, 68.0), "KILL", "kill", 1, 1), H.op_title(2, "Overwatch", "", 5.0),
                             {"kind": "text", "text": "HELLO"}]}
        self.assertEqual(editdiff._overlay_texts(clip), ["Contact 68", "OpTitle OVERWATCH", "HELLO"])

    def test_every_overlay_it_builds_is_a_valid_motion_overlay(self):
        for o in (H.contact(kill(5.0, 12.0), "KILL", "kill", 1, 1), H.op_title(2, "Overwatch", "3 CONTACTS", 5.0)):
            spec = {"name": "x", "output": {"preset": "youtube_1080p30"}, "sources": {"v": {"file": "C:/x.mp4"}},
                    "clips": [{"id": "a", "src": "v", "in": 0.0, "out": 20.0, "overlays": [o]}]}
            E.normalize(spec)


class TestNumbersAreTheEditsOwn(unittest.TestCase):
    def test_op_sub(self):
        self.assertEqual(H.op_sub([kill(1, 25.0), kill(2, 99.4)]), "2 CONTACTS  //  LONGEST 99 M")
        self.assertEqual(H.op_sub([kill(1, 25.0)]), "1 CONTACT  //  LONGEST 25 M")
        self.assertEqual(H.op_sub([kill(1)]), "1 CONTACT")                     # no range read: none claimed
        self.assertEqual(H.op_sub([]), "")

    def test_tally_counts_what_is_in_the_edit(self):
        ks = [kill(1, 10.0, headshot=True), kill(2, 30.0, weapon="grenade"), kill(3, None), kill(4, 20.0)]
        t = H.tally(ks, 3)
        self.assertEqual((t["kills"], t["headshots"], t["nades"], t["far"], t["measured"], t["streams"]), (4, 1, 1, 30.0, 3, 3))
        self.assertAlmostEqual(t["avg"], 20.0)                                  # over the three that have a range, not four

    def test_a_stream_with_no_ranges_gets_no_range_tiles(self):
        card = H.stats_card("AFTER ACTION", 1, H.tally([kill(1), kill(2)], 1), [])
        labels = [s["label"] for s in card["overlays"][0]["props"]["stats"]]
        self.assertNotIn("LONGEST SHOT", labels)
        self.assertNotIn("AVG RANGE", labels)
        self.assertEqual(card["overlays"][0]["props"]["foot"], "AFTER ACTION  //  REPORT 01")

    def test_the_cards_are_motion_cards_that_land_on_their_hit(self):
        t = H.title_card("After Action", 1, "Field report", "Four operations")
        o = t["overlays"][0]
        self.assertEqual((t["type"], o["comp"], o["at"], o["hit"]), ("card", "Title", H.TITLE_HIT, H.TITLE_HIT))
        self.assertEqual(o["props"]["series"], "AFTER ACTION")


LINEUP = {
    "name": "t", "series": "AFTER ACTION", "number": 1, "look": "hud", "profile": "punchy",
    "sources": {"v": {"file": "C:/x.mp4"}},
    "hud_sfx": {"contact": {"asset": "sfx_aaaaaaaaaaaaaaaa", "gain_db": -6}, "title": {"asset": "sfx_bbbbbbbbbbbbbbbb"},
                "op": {"asset": "sfx_cccccccccccccccc"}, "ratchet": {"asset": "sfx_dddddddddddddddd"}, "stamp": {"asset": "sfx_eeeeeeeeeeeeeeee"}},
    "beds": [{"asset": "mus_ffffffffffffffff", "gain_db": -15, "from_chapter": 1, "to_chapter": 2, "fade_in": 1.0}],
    "cold_open": {"clips": [{"src": "v", "in": 10.0, "out": 18.0, "snap": False, "cutaway": True,
                             "motion": [{"comp": "Acquire", "at": 11.0, "dur": 6.0}]}]},
    "sting": {"dur": 3.6},
    "chapters": [
        {"title": "Breach", "clips": [
            {"src": "v", "in": 100.0, "out": 130.0, "kills": [kill(112.0, 4.0, headshot=True), kill(114.5, 10.0)]},
            {"src": "v", "in": 200.0, "out": 220.0, "kills": [kill(211.0, 25.0, weapon="grenade")]}]},
        {"title": "The Shot", "clips": [
            {"src": "v", "in": 300.0, "out": 322.0, "kills": [kill(312.0, 238.0)], "hero": True}]},
    ],
    "end": {"dur": 7.0},
}


def build(**over):
    lineup = json.loads(json.dumps(LINEUP))
    lineup.update(over)
    facts = {}
    return TPL.episode(lineup, facts=facts), facts


class TestEpisodeHudLook(unittest.TestCase):
    def setUp(self):
        self.spec, self.facts = build()
        self.clips = {c["id"]: c for c in self.spec["clips"]}

    def motion(self, cid, comp=None):
        return [o for o in self.clips[cid]["overlays"] if o["kind"] == "motion" and (comp is None or o["comp"] == comp)]

    def test_it_is_a_valid_spec(self):
        E.normalize(self.spec)

    def test_a_kill_gets_a_contact_plate_not_a_still_callout(self):
        ov = self.clips["c1_02"]["overlays"]
        self.assertEqual([o["kind"] for o in ov].count("callout"), 0)
        plates = [o for o in ov if o.get("comp") == "Contact"]
        self.assertEqual([(o["at"], o["props"]["index"], o["props"]["total"]) for o in plates], [(112.0, 1, 4), (114.5, 2, 4)])

    def test_two_kills_closer_than_a_plates_life_stack(self):
        a, b = self.motion("c1_02", "Contact")
        self.assertNotIn("y", a["props"])
        self.assertEqual(b["props"]["y"], 72 + 372)

    def test_double_kill_only_for_kills_a_few_seconds_apart(self):
        near, _ = build(beds=[], chapters=[{"title": "x", "clips": [{"src": "v", "in": 100.0, "out": 130.0,
                                                           "kills": [kill(112.0, 4.0), kill(114.5, 10.0)]}]}])
        far, _ = build(beds=[], chapters=[{"title": "x", "clips": [{"src": "v", "in": 100.0, "out": 130.0,
                                                          "kills": [kill(112.0, 4.0), kill(119.0, 10.0)]}]}])
        heads = lambda spec: [o["props"]["headline"] for c in spec["clips"] for o in c["overlays"] if o.get("comp") == "Contact"]
        self.assertEqual(heads(near), ["KILL CONFIRMED", "DOUBLE KILL"])
        self.assertEqual(heads(far), ["KILL CONFIRMED", "KILL CONFIRMED"])           # seven seconds apart is a run

    def test_a_slow_motion_ramp_stretches_the_gap_so_only_a_very_close_pair_stacks(self):
        spec, _ = build(beds=[], chapters=[{"title": "x", "clips": [{"src": "v", "in": 100.0, "out": 130.0, "kills": [
            kill(112.0, 25.0, weapon="grenade"), kill(114.0, 11.0, headshot=True)]}]}])
        plates = [o for c in spec["clips"] for o in c["overlays"] if o.get("comp") == "Contact"]
        self.assertTrue(any(c.get("speed") for c in spec["clips"]))                 # the nade got its ramp
        self.assertTrue(all("y" not in p["props"] for p in plates))                  # 2 s apart in the source, ~4 s on screen

    def test_the_hero_kill_gets_the_big_plate(self):
        (p,) = self.motion("c2_04", "Contact")
        self.assertTrue(p["props"]["hero"])
        self.assertEqual(p["props"]["distance"], 238)

    def test_the_first_clip_of_a_chapter_carries_the_operation_header_with_the_chapters_own_numbers(self):
        (op,) = self.motion("c1_02", "OpTitle")
        self.assertEqual((op["props"]["op"], op["props"]["title"]), (1, "BREACH"))
        self.assertEqual(op["props"]["sub"], "3 CONTACTS  //  LONGEST 25 M")
        self.assertEqual(op["at"], 100.0 + H.OP_LANDING)
        self.assertFalse(self.motion("c1_03", "OpTitle"))             # only the first clip of a chapter

    def test_the_cold_open_has_no_header_and_takes_the_authors_graphic(self):
        (a,) = self.motion("o01")
        self.assertEqual(a["comp"], "Acquire")
        self.assertFalse(self.motion("o01", "OpTitle"))

    def test_cards_are_the_motion_cards(self):
        self.assertEqual(self.clips["sting"]["overlays"][0]["comp"], "Title")
        stats = self.clips["end"]["overlays"][0]
        self.assertEqual(stats["comp"], "Stats")
        values = {s["label"]: s["value"] for s in stats["props"]["stats"]}
        self.assertEqual((values["KILLS"], values["LONGEST SHOT"], values["HEADSHOTS"], values["GRENADES"], values["STREAMS"]), (4, 238, 1, 1, 1))
        self.assertEqual([(o["n"], o["name"], o["count"]) for o in stats["props"]["ops"]], [(1, "BREACH", 3), (2, "THE SHOT", 1)])

    def test_each_graphic_has_its_sound_on_the_peak(self):
        sfx = {s["id"]: s for s in self.spec["tracks"]["sfx"]}
        self.assertEqual(sfx["ctc1_02_0"]["on"], {"clip": "c1_02", "at": 112.0})
        self.assertEqual(sfx["ctc1_02_0"]["align"], "peak")
        self.assertEqual(sfx["sting"]["on"], {"clip": "sting", "at": H.TITLE_HIT})
        self.assertEqual(sfx["opc1_02"]["on"]["at"], 100.0 + H.OP_LANDING)
        self.assertEqual(sfx["endratchet"]["align"], "start")
        self.assertEqual(sfx["endstamp"]["on"], {"clip": "end", "at": H.STATS_HIT})

    def test_one_bed_spans_the_chapters_it_names(self):
        (bed,) = [m for m in self.spec["tracks"]["music"] if m["id"] == "mbed1"]
        self.assertEqual(bed["start"], {"clip": "c1_02"})
        self.assertEqual(bed["end"], {"clip": "c2_04", "from": "end"})
        self.assertEqual(bed["gain_db"], -15)

    def test_a_bed_naming_a_chapter_that_is_not_there_is_refused(self):
        with self.assertRaises(E.SpecError):
            build(beds=[{"asset": "mus_ffffffffffffffff", "from_chapter": 1, "to_chapter": 9}])

    def test_the_facts_carry_the_tally(self):
        self.assertEqual(self.facts["tally"]["kills"], 4)
        self.assertEqual(self.facts["tally"]["far"], 238.0)

    def test_the_plain_look_is_untouched(self):
        spec, _ = build(look="plain", cold_open={"clips": [{"src": "v", "in": 10.0, "out": 18.0, "snap": False}]})
        kinds = {o["kind"] for c in spec["clips"] for o in c["overlays"]}
        self.assertNotIn("motion", kinds)
        self.assertIn("callout", kinds)

    def test_graphics_are_events_so_a_kill_clip_has_no_dead_stretch(self):
        tl = T.compile_timeline(E.normalize(self.spec))
        self.assertEqual([i for i in T.pacing(tl)["issues"] if i["what"] == "dead"], [])


class TestKillIds(unittest.TestCase):
    def test_a_window_chosen_by_hand_takes_its_kills_from_the_feed_by_id(self):
        lineup = {"sources": {"v": {}}, "chapters": [{"title": "x", "clips": [{"src": "v", "in": 0, "out": 30, "kill_ids": ["k1", "k2"]}]}]}
        known = {"k1": kill(10.0, 5.0), "k2": kill(20.0, 9.0)}
        done = TPL.expand_moments(lineup, lambda a: None, lambda a: known)
        self.assertEqual([k["t"] for k in lineup["chapters"][0]["clips"][0]["kills"]], [10.0, 20.0])
        self.assertIn("2 kill(s) by id", done[0])

    def test_an_unknown_id_is_refused_by_name(self):
        lineup = {"sources": {"v": {}}, "chapters": [{"title": "x", "clips": [{"src": "v", "in": 0, "out": 30, "kill_ids": ["k9"]}]}]}
        with self.assertRaises(E.SpecError) as cm:
            TPL.expand_moments(lineup, lambda a: None, lambda a: {})
        self.assertIn("k9", str(cm.exception))


class TestHudSounds(unittest.TestCase):
    def test_each_cue_peaks_where_its_graphic_lands(self):
        want = {"hud_contact": 0.55, "hud_slam": 1.0, "hud_op": 0.5, "hud_stamp": 0.0}
        for cid, t in want.items():
            x = S.CUES[cid]["fn"]()
            self.assertAlmostEqual(int(np.argmax(np.abs(x))) / S.SR, t, delta=0.06, msg=cid)

    def test_no_cue_clips_or_is_silent_and_each_is_deterministic(self):
        for cid, cue in S.CUES.items():
            a, b = cue["fn"](), cue["fn"]()
            self.assertTrue(np.array_equal(a, b), cid)
            self.assertLessEqual(float(np.abs(a).max()), 0.9, cid)
            self.assertGreater(float(np.sqrt((a ** 2).mean())), 0.02, cid)

    def test_the_contact_cue_gets_sparser_toward_the_lock(self):
        x = np.abs(S.hud_contact())
        win = lambda a, b: float(x[int(a * S.SR):int(b * S.SR)].max())
        self.assertGreater(win(0.5, 0.6), 2 * win(0.0, 0.1))        # the lock is much louder than the first tick

    def test_the_library_files_are_stereo_48k_with_a_cc0_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            names = S.write_library(Path(d))
            self.assertEqual(len(names), len(S.CUES))
            with wave.open(str(Path(d) / "hud_contact.wav")) as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (2, 48000, 2))
            side = json.loads((Path(d) / "hud_contact.wav.json").read_text(encoding="utf-8"))
            self.assertEqual(side["license"]["name"], "CC0-1.0")
            self.assertIn("hud", side["tags"])


if __name__ == "__main__":
    unittest.main()
