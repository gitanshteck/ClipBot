"""Tests for clipbot/edittemplates.py (the kill-montage generator) and edittaste.py."""

import json
import tempfile
import unittest
from pathlib import Path

from clipbot import editspec as E
from clipbot import edittaste as TASTE
from clipbot import edittemplates as TPL
from clipbot import edittimeline as T

SOURCES = {"v1": {"file": "D:/a.mp4"}, "v2": {"file": "D:/b.mp4"}}


def kill(t, src="v1", dist=15.0, weapon="gun", headshot=False, victim="Lotte", icon=None):
    return {"src": src, "t": float(t), "dist_m": dist, "weapon": weapon, "headshot": headshot, "through_cover": False,
            "victim": victim, "icon": icon}


KILLS = [
    kill(100, dist=15),                                       # plain: no chapter
    kill(300, dist=238, headshot=True, victim="KULLER"),      # headshot (and long)
    kill(500, dist=60),                                       # 50 m+
    kill(700, dist=120),                                      # 100 m+
    kill(900, dist=10), kill(906, dist=12), kill(912, dist=8),  # a run -> multi
    kill(1500, dist=20, weapon="grenade", victim="Nader"),    # nade
]


def build(kills=KILLS, **kw):
    return TPL.kill_montage(kills, SOURCES, name="m", player="Teck", game="Wardogs", **kw)


class TestSegments(unittest.TestCase):
    def test_close_kills_share_a_clip(self):
        segs = TPL._segments([kill(900), kill(906), kill(1200)], E.PROFILES["punchy"])
        self.assertEqual([len(s["kills"]) for s in segs], [2, 1])

    def test_kills_in_different_sources_never_merge(self):
        segs = TPL._segments([kill(900, src="v1"), kill(902, src="v2")], E.PROFILES["punchy"])
        self.assertEqual(len(segs), 2)

    def test_the_window_follows_the_profile_and_grenades_get_more_room(self):
        p = E.PROFILES["punchy"]
        plain = TPL._segments([kill(100)], p)[0]
        nade = TPL._segments([kill(100, weapon="grenade")], p)[0]
        self.assertAlmostEqual(plain["out"] - plain["in"], p["pre_roll"] + p["post_roll"])
        self.assertGreater(nade["out"] - nade["in"], plain["out"] - plain["in"])

    def test_the_window_cannot_start_before_zero(self):
        self.assertEqual(TPL._segments([kill(2)], E.PROFILES["punchy"])[0]["in"], 0.0)

    def test_scores_rank_what_the_viewer_cares_about(self):
        self.assertGreater(TPL.kill_score(kill(1, weapon="grenade")), TPL.kill_score(kill(1, headshot=True)))
        self.assertGreater(TPL.kill_score(kill(1, headshot=True)), TPL.kill_score(kill(1)))
        self.assertGreater(TPL.kill_score(kill(1, dist=150)), TPL.kill_score(kill(1, dist=60)))
        self.assertGreater(TPL.kill_score(kill(1, dist=60)), TPL.kill_score(kill(1, dist=10)))


class TestChapters(unittest.TestCase):
    def test_a_run_of_kills_is_a_multi_kill_chapter(self):
        segs = TPL._segments(KILLS, E.PROFILES["punchy"])
        names = [TPL._chapter(s) for s in segs]
        self.assertIn("MULTI-KILLS & RUNS", names)
        self.assertEqual(names[-1], "NADES")

    def test_plain_kills_have_no_chapter(self):
        segs = TPL._segments([kill(100, dist=10)], E.PROFILES["punchy"])
        self.assertIsNone(TPL._chapter(segs[0]))

    def test_chapter_order_builds_to_the_nades(self):
        segs = TPL._segments(KILLS, E.PROFILES["punchy"])
        ordered = [TPL._chapter(s) for s in TPL.order_segments(segs, "chapters", None)]
        self.assertEqual(ordered, sorted(ordered, key=TPL.CHAPTER_ORDER.index))
        self.assertEqual(ordered[-1], "NADES")

    def test_chronological_and_score_orders(self):
        segs = TPL._segments(KILLS, E.PROFILES["punchy"])
        chrono = TPL.order_segments(segs, "chronological", None)
        self.assertEqual([s["kills"][0]["t"] for s in chrono], sorted(s["kills"][0]["t"] for s in chrono))
        scored = TPL.order_segments(segs, "score", None)
        self.assertEqual([s["score"] for s in scored], sorted(s["score"] for s in scored))

    def test_a_limit_keeps_the_best(self):
        segs = TPL._segments(KILLS, E.PROFILES["punchy"])
        kept = TPL.order_segments(segs, "score", 2)
        top = sorted(segs, key=lambda s: -s["score"])[:2]
        self.assertEqual({s["id"] for s in kept}, {s["id"] for s in top})


class TestLabels(unittest.TestCase):
    def test_nade_headshot_and_distance(self):
        self.assertEqual(TPL._label(kill(1, weapon="grenade"), 1)[0], "NADE KILL")
        self.assertEqual(TPL._label(kill(1, headshot=True), 1)[0], "HEADSHOT")
        self.assertEqual(TPL._label(kill(1, dist=120), 1)[0], "120 M SHOT")
        self.assertEqual(TPL._label(kill(1, dist=60), 1)[0], "60 M")
        self.assertEqual(TPL._label(kill(1, dist=10), 1)[0], "KILL")

    def test_the_count_builds_across_a_clip(self):
        self.assertEqual(TPL._label(kill(1), 2)[0], "DOUBLE KILL")
        self.assertEqual(TPL._label(kill(1), 3)[0], "TRIPLE KILL")
        self.assertEqual(TPL._label(kill(1), 7)[0], "7-KILL")

    def test_the_sub_line_carries_distance_and_victim(self):
        self.assertEqual(TPL._label(kill(1, dist=17, victim="KULLER"), 1)[1], "17 m  KULLER")
        self.assertEqual(TPL._label(kill(1, dist=None, victim=""), 1)[1], "")


class TestMontageSpec(unittest.TestCase):
    def test_the_spec_validates_and_compiles(self):
        spec = E.normalize(build())
        tl = T.compile_timeline(spec)
        self.assertGreater(tl["duration"], 20)
        self.assertEqual(tl["warnings"], [])

    def test_positions_are_source_seconds_inside_the_window(self):
        spec = build()
        for c in spec["clips"]:
            if c.get("type", "clip") != "clip":
                continue
            for m in c["markers"]:
                self.assertTrue(c["in"] <= m["at"] <= c["out"])
            for o in c["overlays"]:
                self.assertTrue(c["in"] <= o["at"] <= c["out"])

    def test_every_kill_gets_a_marker_a_callout_and_a_punch(self):
        spec = E.normalize(build([kill(100, dist=60)]))
        c = spec["clips"][1]
        self.assertEqual(len(c["markers"]), 1)
        self.assertEqual(c["overlays"][0]["kind"], "callout")
        self.assertTrue(any(k["zoom"] > 1.0 for k in c["camera"]))

    def test_camera_keys_always_rise_in_time(self):
        spec = E.normalize(build([kill(100), kill(100.8), kill(101.5), kill(300, dist=150)], order="chronological"))
        for c in spec["clips"]:
            times = [k["at"] for k in c.get("camera", [])]
            self.assertEqual(times, sorted(times))
            self.assertEqual(len(times), len(set(times)))

    def test_a_long_lead_gets_a_slow_push_in(self):
        spec = build([kill(100, dist=60)])
        zooms = [k["zoom"] for k in spec["clips"][1]["camera"]]
        self.assertGreater(len(zooms), 4)
        self.assertGreater(max(zooms[:-3]), 1.0)     # drift before the punch

    def test_the_montage_passes_its_own_pacing_gate(self):
        tl = T.compile_timeline(E.normalize(build()))
        self.assertEqual(T.pacing(tl)["dead"], [])

    def test_slowmo_only_on_grenades_by_default(self):
        spec = build()
        slowed = [c for c in spec["clips"] if c.get("speed")]
        self.assertEqual(len(slowed), 1)
        self.assertIn("nade", slowed[0]["tags"])
        self.assertTrue(all(k["rate"] <= 1.0 for k in slowed[0]["speed"]))

    def test_slowmo_options(self):
        none = build(slowmo="none")
        self.assertFalse(any(c.get("speed") for c in none["clips"]))
        allc = build(slowmo="all", order="chronological")
        self.assertTrue(all(c.get("speed") for c in allc["clips"] if c.get("type", "clip") == "clip"))

    def test_the_slowmo_ramp_stays_inside_its_clip(self):
        for c in build(slowmo="all")["clips"]:
            for k in c.get("speed", []):
                self.assertTrue(c["in"] <= k["at"] <= c["out"])

    def test_grenades_and_headshots_flash(self):
        spec = build()
        flashed = {c["id"] for c in spec["clips"] if c.get("fx")}
        self.assertTrue(any("nade" in c["tags"] and c["id"] in flashed for c in spec["clips"]))

    def test_chapters_follow_the_chapter_groups(self):
        spec = E.normalize(build())
        ch = T.chapters(T.compile_timeline(spec))
        self.assertEqual(ch[0]["t"], 0.0)
        self.assertTrue(ch[0]["title"].startswith("Intro"))        # a 2 s card folds into the first chapter
        self.assertEqual(ch[-1]["title"], "Nades")
        self.assertTrue(all(c["t"] >= 10.0 for c in ch[1:]))

    def test_intro_and_outro_are_optional(self):
        spec = build(intro=False, outro=False)
        self.assertEqual({c.get("type", "clip") for c in spec["clips"]}, {"clip"})

    def test_music_sfx_and_ducking(self):
        spec = E.normalize(build(
            music={"asset": "mus_3a25c534c578d312", "gain_db": -18.0},
            sfx={"hit": {"asset": "sfx_08a073fd8c00c828"}, "nade": {"asset": "sfx_ece66431f90a41f6"}}))
        self.assertEqual(len(spec["tracks"]["music"]), 1)
        self.assertTrue(spec["tracks"]["music"][0]["duck"]["enabled"])
        self.assertEqual(len(spec["tracks"]["sfx"]), 7)       # one per kill that qualified for a chapter
        self.assertIn("sfx_ece66431f90a41f6", [s["asset"] for s in spec["tracks"]["sfx"]])

    def test_sfx_land_on_their_kill(self):
        spec = E.normalize(build([kill(100, dist=60)], sfx={"hit": {"asset": "sfx_08a073fd8c00c828"}}))
        tl = T.compile_timeline(spec)
        marker = tl["clips"][1]["start"] + tl["clips"][1]["markers"][0]["at"]
        self.assertAlmostEqual(tl["tracks"]["sfx"][0]["hit"], marker, places=2)

    def test_facts_describe_the_edit_not_the_input(self):
        facts = {}
        build(facts=facts)                         # KILLS has 8 kills; the plain one has no chapter and is left out
        self.assertEqual((facts["kills"], facts["nades"], facts["far"]), (7, 1, 238.0))
        facts = {}
        build(order="chronological", facts=facts)
        self.assertEqual(facts["kills"], 8)
        facts = {}
        build([kill(100, dist=None, headshot=True)], facts=facts)
        self.assertIsNone(facts["far"])

    def test_no_kills_is_an_error(self):
        with self.assertRaises(E.SpecError):
            build([])
        with self.assertRaises(E.SpecError):
            build([kill(100, dist=10)], order="chapters")      # nothing qualifies for a chapter

    def test_max_clips_limits_the_edit(self):
        spec = build(max_clips=3, intro=False, outro=False)
        self.assertEqual(len(spec["clips"]), 3)

    def test_two_sources_keep_their_aliases(self):
        spec = build([kill(100, src="v1", dist=60), kill(100, src="v2", dist=60)], order="chronological")
        self.assertEqual({c["src"] for c in spec["clips"] if c.get("type", "clip") == "clip"}, {"v1", "v2"})

    def test_overlapping_callouts_are_stacked(self):
        spec = build([kill(100), kill(101)], order="chronological")
        ys = [o["y"] for o in spec["clips"][1]["overlays"]]
        self.assertEqual(len(set(ys)), 2)


class TestKillsFromKillfeed(unittest.TestCase):
    def test_icons_are_made_absolute_and_missing_ones_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "killfeed_icons").mkdir()
            (Path(d) / "killfeed_icons" / "k001.png").write_bytes(b"x")
            data = {"events": [
                {"t": 10.0, "dist_m": 15.0, "weapon": "gun", "victim": "Lotte", "icon": "killfeed_icons/k001.png"},
                {"t": 20.0, "dist_m": None, "weapon": "grenade", "headshot": True, "icon": "killfeed_icons/k002.png"},
                {"t": 30.0, "dist_m": 9.0, "weapon": "gun", "victim": "(HUNT)MMRÅ+O"}]}
            kills = TPL.kills_from_killfeed(data, "v1", d)
        self.assertTrue(kills[0]["icon"].endswith("k001.png"))
        self.assertIsNone(kills[1]["icon"])
        self.assertEqual(kills[1]["weapon"], "grenade")
        self.assertTrue(kills[1]["headshot"])
        self.assertEqual(kills[0]["src"], "v1")
        self.assertEqual(kills[0]["victim"], "Lotte")
        self.assertEqual(kills[2]["victim"], "")                # OCR noise is blanked, not shown


class TestTaste(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "taste.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_store(self):
        self.assertEqual(TASTE.active(self.path), [])

    def test_add_list_retire(self):
        r1 = TASTE.add(self.path, "never zoom on the webcam", said="stop zooming at my face", edit="teck-reel", now=0)
        r2 = TASTE.add(self.path, "callouts bottom-left")
        self.assertEqual((r1["id"], r2["id"]), ("t1", "t2"))
        self.assertEqual([r["id"] for r in TASTE.active(self.path)], ["t1", "t2"])
        TASTE.retire(self.path, "t1")
        self.assertEqual([r["id"] for r in TASTE.active(self.path)], ["t2"])
        self.assertEqual(len(TASTE.load(self.path)["rules"]), 2)    # retired, not deleted

    def test_ids_never_reuse(self):
        TASTE.add(self.path, "a")
        TASTE.add(self.path, "b")
        TASTE.retire(self.path, "t2")
        self.assertEqual(TASTE.add(self.path, "c")["id"], "t3")

    def test_the_users_words_are_kept(self):
        TASTE.add(self.path, "no whoosh", said="I hate the whoosh")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["rules"][0]["said"], "I hate the whoosh")

    def test_errors(self):
        with self.assertRaises(E.SpecError):
            TASTE.add(self.path, "   ")
        with self.assertRaises(E.SpecError):
            TASTE.retire(self.path, "t9")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1]", encoding="utf-8")
        with self.assertRaises(E.SpecError):
            TASTE.load(self.path)


if __name__ == "__main__":
    unittest.main()
