"""The `hl` look (TECK HIGHLIGHTS): the overlay builders, the episode template in `look: "hl"`, and what a kill plate says."""

import json
import unittest

from clipbot import edithl as H
from clipbot import editspec as E, edittemplates as TPL, edittimeline as T


def kill(t, **kw):
    return dict({"id": "k%d" % int(t), "t": float(t), "dist_m": None, "weapon": "gun", "headshot": False, "through_cover": False,
                 "victim": "", "icon": None}, **kw)


def popup(kind="KILL", medals=(), situations=(), multi=None):
    return {"kind": kind, "medals": list(medals), "situations": list(situations), "objective": ["DEFENSE"], "multi": multi,
            "support": ["UAV REVEAL ASSIST"], "lines": []}


class TestPlate(unittest.TestCase):
    def test_a_plate_says_what_the_game_said_and_lands_on_the_kill(self):
        k = kill(100.0, popup=popup("HEADSHOT KILL", ["QUICK SCOPE", "ONE SHOT, ONE KILL"], ["REVENGE"]))
        o = H.kill_plate(k, 3, 14)
        self.assertEqual((o["kind"], o["comp"], o["at"], o["hit"]), ("motion", "HlKill", 100.0, H.KILL_HIT))
        p = o["props"]
        self.assertEqual((p["kind"], p["index"], p["total"]), ("HEADSHOT KILL", 3, 14))
        self.assertEqual(p["tags"], ["QUICK SCOPE", "ONE SHOT, ONE KILL", "REVENGE"])

    def test_medals_are_ordered_by_how_much_they_are_worth_and_capped(self):
        tags = H.popup_tags(popup(medals=["POINT BLANK", "LONGSHOT", "ONE SHOT, ONE KILL"], situations=["SURVIVOR", "REVENGE"]))
        self.assertEqual(tags, ["LONGSHOT", "ONE SHOT, ONE KILL", "POINT BLANK"])
        self.assertEqual(len(tags), H.MAX_TAGS)

    def test_the_weakest_calls_of_the_reader_are_not_drawn(self):
        # OFFENSE / DEFENSE are read at ~85% and a support line is not about the kill: neither becomes a chip
        tags = H.popup_tags(popup(medals=["QUICK SCOPE"]))
        self.assertEqual(tags, ["QUICK SCOPE"])
        self.assertEqual(H.popup_tags(None), [])
        self.assertEqual(H.popup_tags({}), [])

    def test_a_kill_with_no_popup_gets_what_the_feed_implies_and_no_medals(self):
        self.assertEqual(H.kill_plate(kill(5.0), 1, 1)["props"]["kind"], "KILL")
        hs = H.kill_plate(kill(5.0, headshot=True), 1, 1)["props"]
        self.assertEqual((hs["kind"], hs["tags"]), ("HEADSHOT KILL", []))

    def test_a_popup_whose_kind_line_was_not_read_says_plain_kill_not_what_the_feeds_skull_guessed(self):
        # k007: the popup carried its medals but no kind, the feed row had a skull, and the game had printed KILL
        k = kill(5.0, headshot=True, popup=popup(None, ["QUICK SCOPE"]))
        self.assertEqual(H.kill_plate(k, 1, 1)["props"]["kind"], "KILL")
        self.assertEqual(H.kill_plate(k, 1, 1)["props"]["tags"], ["QUICK SCOPE"])

    def test_the_games_multi_kill_line_wins_over_a_count_made_here(self):
        self.assertEqual(H.kill_plate(kill(5.0, popup=popup(multi="DOUBLE KILL")), 1, 2, chain=1)["props"]["multi"], "DOUBLE KILL")
        # with no popup read, a run of kills a few seconds apart is still named
        self.assertEqual(H.kill_plate(kill(5.0), 1, 2, chain=2)["props"]["multi"], "DOUBLE KILL")
        self.assertNotIn("multi", H.kill_plate(kill(5.0), 1, 2, chain=1)["props"])
        # but a kill whose popup WAS read and has no multi line is not called a double on a guess
        self.assertNotIn("multi", H.kill_plate(kill(5.0, popup=popup()), 1, 2, chain=2)["props"])

    def test_a_plate_gives_way_to_the_next_kill(self):
        self.assertEqual(H.plate_durations([10.0, 12.0, 30.0]), [1.95, H.KILL_DUR, H.KILL_DUR])
        self.assertEqual(H.plate_durations([10.0, 10.4]), [H.KILL_MIN_DUR, H.KILL_DUR])
        self.assertEqual(H.plate_durations([10.0]), [H.KILL_DUR])

    def test_every_overlay_it_builds_is_a_valid_motion_overlay(self):
        built = [H.kill_plate(kill(5.0, popup=popup("KILL", ["QUICK SCOPE"])), 1, 1), H.chapter_header(2, "Quickscope era", "4 kills", 5.0),
                 H.tag("The Goo killcam", 5.0, sub="best play, not his", tone="gold", y=300)]
        for o in built:
            spec = {"name": "x", "output": {"preset": "youtube_1080p30"}, "sources": {"v": {"file": "C:/x.mp4"}},
                    "clips": [{"id": "a", "src": "v", "in": 0.0, "out": 20.0, "overlays": [o]}]}
            E.normalize(spec)


class TestNumbers(unittest.TestCase):
    def test_tally_counts_the_edits_own_kills(self):
        ks = [kill(1, popup=popup("HEADSHOT KILL", ["QUICK SCOPE"])), kill(2, popup=popup("KILL", ["QUICK SCOPE", "LONGSHOT"])),
              kill(3, headshot=True), kill(4)]
        self.assertEqual(H.tally(ks), {"kills": 4, "headshots": 2, "quickscopes": 2, "longshots": 1})

    def test_a_headshot_the_feed_saw_counts_even_when_the_popup_only_said_kill(self):
        # the popup is read more often than the feed's skull, but either one is a headshot
        self.assertEqual(H.tally([kill(1, headshot=True, popup=popup("KILL")), kill(2, popup=popup("HEADSHOT KILL")), kill(3, popup=popup("KILL"))])["headshots"], 2)

    def test_the_end_card_leaves_out_a_count_of_zero(self):
        self.assertEqual([s["label"] for s in H.default_stats(H.tally([kill(1), kill(2)]))], ["KILLS"])
        card = H.end_card(H.default_stats({"kills": 9, "headshots": 3, "quickscopes": 0, "longshots": 1}), "see you next stream")
        props = card["overlays"][0]["props"]
        self.assertEqual([(s["label"], s["value"]) for s in props["stats"]], [("KILLS", 9), ("HEADSHOTS", 3), ("LONGSHOTS", 1)])
        self.assertEqual(props["line"], "SEE YOU NEXT STREAM")
        self.assertEqual((card["type"], card["overlays"][0]["comp"], card["overlays"][0]["at"]), ("card", "HlEnd", H.END_HIT))

    def test_the_nights_numbers_come_from_the_whole_feed(self):
        doc = {"events": [{"popup": popup("HEADSHOT KILL", ["QUICK SCOPE"]), "headshot": True}, {"popup": popup()}, {"popup": None, "headshot": True}]}
        self.assertEqual(H.night(doc), {"kills": 3, "headshots": 2, "quickscopes": 1, "longshots": 0})

    def test_only_four_tiles(self):
        self.assertEqual(len(H.end_card([{"label": str(i), "value": i} for i in range(7)], "x")["overlays"][0]["props"]["stats"]), H.MAX_STATS)

    def test_the_title_card_is_a_motion_card_that_lands_on_its_hit(self):
        t = H.title_card("Teck Highlights", "Modern Warfare 4", "Day one", "Day 01")
        o = t["overlays"][0]
        self.assertEqual((t["type"], o["comp"], o["at"], o["hit"]), ("card", "HlTitle", H.TITLE_HIT, H.TITLE_HIT))
        self.assertEqual((o["props"]["series"], o["props"]["tag"]), ("TECK HIGHLIGHTS", "DAY 01"))


LINEUP = {
    "name": "t", "series": "TECK HIGHLIGHTS", "number": None, "look": "hl", "profile": "balanced",
    "sources": {"v": {"file": "C:/x.mp4"}},
    "hl": {"kicker": "Modern Warfare 4", "sub": "Day one", "tag": "Day 01", "end_line": "See you next stream"},
    "hl_sfx": {"kill": {"asset": "sfx_aaaaaaaaaaaaaaaa", "gain_db": -6}, "title": {"asset": "sfx_bbbbbbbbbbbbbbbb"},
               "chapter": {"asset": "sfx_cccccccccccccccc"}, "end": {"asset": "sfx_dddddddddddddddd"}},
    "cold_open": {"clips": [{"src": "v", "in": 10.0, "out": 18.0, "snap": False}]},
    "sting": {"dur": 3.2},
    "chapters": [
        {"title": "Quickscope era", "clips": [
            {"src": "v", "in": 100.0, "out": 130.0, "label": "Day one, bro",
             "kills": [kill(112.0, popup=popup("HEADSHOT KILL", ["QUICK SCOPE"])), kill(114.5, popup=popup("KILL", multi="DOUBLE KILL"))]},
            {"src": "v", "in": 200.0, "out": 220.0, "kills": [kill(211.0)]}]},
        {"title": "Talk", "sub": "no kills here", "clips": [{"src": "v", "in": 300.0, "out": 322.0, "label": "The call"}]},
    ],
    "end": {"dur": 4.0},
}


def build(**over):
    lineup = json.loads(json.dumps(LINEUP))
    lineup.update(over)
    facts = {}
    return TPL.episode(lineup, facts=facts), facts


class TestEpisodeHlLook(unittest.TestCase):
    def setUp(self):
        self.spec, self.facts = build()
        self.clips = {c["id"]: c for c in self.spec["clips"]}

    def motion(self, cid, comp=None):
        return [o for o in self.clips[cid]["overlays"] if o["kind"] == "motion" and (comp is None or o["comp"] == comp)]

    def test_it_is_a_valid_spec(self):
        E.normalize(self.spec)

    def test_a_kill_gets_a_plate_not_a_still_callout(self):
        ov = self.clips["c1_02"]["overlays"]
        self.assertEqual([o["kind"] for o in ov].count("callout"), 0)
        plates = [o for o in ov if o.get("comp") == "HlKill"]
        self.assertEqual([(o["at"], o["props"]["index"], o["props"]["total"]) for o in plates], [(112.0, 1, 3), (114.5, 2, 3)])
        self.assertEqual([o["props"]["kind"] for o in plates], ["HEADSHOT KILL", "KILL"])
        self.assertEqual(plates[1]["props"]["multi"], "DOUBLE KILL")

    def test_the_first_plate_gives_way_to_the_second(self):
        a, b = self.motion("c1_02", "HlKill")
        self.assertLess(a["dur"], b["dur"])
        self.assertLessEqual(a["at"] + a["dur"], b["at"] + 0.01 + H.KILL_MIN_DUR)
        self.assertNotIn("y", a["props"])

    def test_the_first_clip_of_a_chapter_carries_the_header_with_the_chapters_own_count(self):
        (ch,) = self.motion("c1_02", "HlChapter")
        self.assertEqual((ch["props"]["n"], ch["props"]["title"], ch["props"]["sub"]), (1, "QUICKSCOPE ERA", "3 KILLS"))
        self.assertEqual(ch["at"], 100.0 + H.CHAPTER_LANDING)
        self.assertFalse(self.motion("c1_03", "HlChapter"))

    def test_a_chapter_can_give_its_own_sub_line_and_a_chapter_of_talk_has_none_by_default(self):
        (ch,) = self.motion("c2_04", "HlChapter")
        self.assertEqual(ch["props"]["sub"], "NO KILLS HERE")
        spec, _ = build(chapters=[{"title": "Talk", "clips": [{"src": "v", "in": 300.0, "out": 322.0}]}])
        (ch,) = [o for c in spec["clips"] for o in c["overlays"] if o.get("comp") == "HlChapter"]
        self.assertNotIn("sub", ch["props"])

    def test_a_clips_label_is_a_tag_and_moves_down_when_it_shares_the_corner_with_a_header(self):
        (tag,) = self.motion("c1_02", "HlTag")
        self.assertEqual((tag["props"]["text"], tag["props"]["y"]), ("DAY ONE, BRO", H.BELOW_CHAPTER_Y))
        spec, _ = build(cold_open={"clips": [{"src": "v", "in": 10.0, "out": 18.0, "label": "Hello", "snap": False}]})
        (cold,) = [o for o in spec["clips"][0]["overlays"] if o.get("comp") == "HlTag"]
        self.assertNotIn("y", cold["props"])

    def test_the_cold_open_has_no_chapter_header(self):
        self.assertFalse(self.motion("o01", "HlChapter"))

    def test_cards_are_the_motion_cards(self):
        self.assertEqual(self.clips["sting"]["overlays"][0]["comp"], "HlTitle")
        end = self.clips["end"]["overlays"][0]
        self.assertEqual(end["comp"], "HlEnd")
        self.assertEqual({s["label"]: s["value"] for s in end["props"]["stats"]}, {"KILLS": 3, "HEADSHOTS": 1, "QUICK SCOPES": 1})
        self.assertEqual(end["props"]["line"], "SEE YOU NEXT STREAM")

    def test_the_author_can_give_the_end_card_the_nights_own_numbers(self):
        spec, _ = build(hl=dict(LINEUP["hl"], stats=[{"label": "KILLS", "value": 239}, {"label": "HEADSHOTS", "value": 62}]))
        end = [c for c in spec["clips"] if c["id"] == "end"][0]["overlays"][0]
        self.assertEqual([(s["label"], s["value"]) for s in end["props"]["stats"]], [("KILLS", 239), ("HEADSHOTS", 62)])

    def test_each_graphic_has_its_sound_on_the_peak(self):
        sfx = {s["id"]: s for s in self.spec["tracks"]["sfx"]}
        self.assertEqual(sfx["plc1_02_0"]["on"], {"clip": "c1_02", "at": 112.0})
        self.assertEqual(sfx["plc1_02_0"]["align"], "peak")
        self.assertEqual(sfx["sting"]["on"], {"clip": "sting", "at": H.TITLE_HIT})
        self.assertEqual(sfx["chc1_02"]["on"]["at"], 100.0 + H.CHAPTER_LANDING)
        self.assertEqual(sfx["endstat"]["on"], {"clip": "end", "at": H.END_HIT})

    def test_the_plain_look_and_the_hud_look_are_untouched(self):
        spec, _ = build(look="plain")
        kinds = {o["kind"] for c in spec["clips"] for o in c["overlays"]}
        self.assertNotIn("motion", kinds)
        self.assertIn("callout", kinds)

    def test_a_kill_with_a_close_neighbour_still_passes_the_pacing_gate(self):
        tl = T.compile_timeline(E.normalize(self.spec))
        self.assertEqual([i for i in T.pacing(tl)["issues"] if i["what"] == "dead"], [])


class TestPerClipSlowmo(unittest.TestCase):
    def clip(self, **extra):
        c = dict({"src": "v", "in": 100.0, "out": 130.0, "kills": [kill(112.0, headshot=True, popup=popup("HEADSHOT KILL"))]}, **extra)
        spec, _ = build(slowmo="special", chapters=[{"title": "x", "clips": [c]}])
        return [c_ for c_ in spec["clips"] if c_["id"] == "c1_02"][0]

    def test_a_headshot_clip_gets_its_ramp_unless_the_clip_says_none(self):
        self.assertTrue(self.clip().get("speed"))
        self.assertFalse(self.clip(slowmo="none").get("speed"))                  # a montage of kills plays at speed

    def test_the_plates_and_marker_are_still_there_without_the_ramp(self):
        c = self.clip(slowmo="none")
        self.assertEqual([o["comp"] for o in c["overlays"] if o["kind"] == "motion"], ["HlChapter", "HlKill"])
        self.assertEqual([m["name"] for m in c["markers"]], ["kill"])


class TestKillsCarryThePopup(unittest.TestCase):
    def test_the_feed_adapter_keeps_the_popup_and_leaves_other_kills_exactly_as_they_were(self):
        doc = {"events": [{"id": "k1", "t": 5.0, "popup": popup("KILL", ["LONGSHOT"])}, {"id": "k2", "t": 9.0, "dist_m": 70}]}
        a, b = TPL.kills_from_killfeed(doc, "v")
        self.assertEqual(a["popup"]["medals"], ["LONGSHOT"])
        self.assertNotIn("popup", b)                       # a Wardogs kill has no popup key at all


if __name__ == "__main__":
    unittest.main()
