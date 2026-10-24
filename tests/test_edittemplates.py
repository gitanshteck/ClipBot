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


def lineup(**kw):
    base = {
        "name": "ep", "series": "TECK MISH MASH", "number": 1, "profile": "restrained",
        "sources": {"a": {"file": "D:/a.mp4"}, "b": {"file": "D:/b.mp4"}},
        "sfx": {"sting": {"asset": "sfx_90f780eeb23c6778"}, "chapter": {"asset": "sfx_1a98e97b627abc75"}},
        "kill_sfx": {"hit": {"asset": "sfx_08a073fd8c00c828"}},
        "cold_open": {"title": "Cold open", "whoosh": False, "clips": [{"src": "a", "in": 100.0, "out": 125.0, "punch": [118.0]}]},
        "sting": {"dur": 3.0},
        "chapters": [
            {"title": "Mortars", "clips": [{"src": "a", "in": 300.0, "out": 312.0, "label": "ACT 1"},
                                          {"src": "b", "in": 50.0, "out": 58.0}]},
            {"title": "Receipts", "music": {"asset": "mus_3a25c534c578d312", "gain_db": -18.0},
             "clips": [{"src": "b", "in": 900.0, "out": 912.0, "kills": [kill(906, src="b", dist=120)]}]},
            {"title": "Dance", "clips": [{"src": "a", "in": 700.0, "out": 716.0}]},
        ],
        "end": {"dur": 6.0, "music": {"asset": "mus_1f311273f96350f0"}},
    }
    base.update(kw)
    return base


class TestEpisode(unittest.TestCase):
    def build(self, **kw):
        facts = {}
        spec = E.normalize(TPL.episode(lineup(**kw), facts=facts))
        return spec, T.compile_timeline(spec), facts

    def test_the_running_order(self):
        spec, tl, _ = self.build()
        ids = [c["id"] for c in spec["clips"]]
        self.assertEqual(ids[0][0], "o")                                # the cold open comes first
        self.assertEqual(ids[1], "sting")
        self.assertEqual(ids[-1], "end")
        self.assertEqual([c["type"] for c in spec["clips"] if c["type"] == "card"], ["card", "card"])

    def test_chapters_are_the_chapter_clips_and_qualify_for_youtube(self):
        from clipbot import editdeliver as D
        spec, tl, _ = self.build()
        ch = T.chapters(tl)
        self.assertEqual([c["title"] for c in ch], ["Cold open", "Mortars", "Receipts", "Dance"])
        self.assertTrue(D.chapter_lines(ch, tl["duration"])["qualifies"])

    def test_cards_are_not_chapters(self):
        spec, _, _ = self.build()
        for c in spec["clips"]:
            if c["type"] == "card":
                self.assertIn("nochapter", c["tags"])

    def test_a_chapter_gets_a_lower_third_and_a_whoosh_but_the_cold_open_does_not(self):
        spec, tl, _ = self.build()
        first = next(c for c in spec["clips"] if c.get("label") == "Mortars")
        texts = [o["text"] for o in first["overlays"]]
        self.assertIn("MORTARS", texts)
        self.assertIn("ACT 1", texts)                                    # the clip's own line, stacked under it
        ys = sorted(o["y"] for o in first["overlays"])
        self.assertLess(ys[0], ys[1])
        self.assertEqual(spec["clips"][0]["overlays"], [])               # cold open: nothing on top of the hook
        whooshes = [s for s in spec["tracks"]["sfx"] if s["id"].startswith("w")]
        self.assertEqual(len(whooshes), 3)
        self.assertFalse(any(s["on"]["clip"] == spec["clips"][0]["id"] for s in whooshes))

    def test_a_clip_too_short_for_a_label_gets_none(self):
        spec, _, _ = self.build(chapters=[{"title": "Quick", "clips": [{"src": "a", "in": 10.0, "out": 11.5, "label": "X"}]}])
        self.assertEqual([c for c in spec["clips"] if c.get("label") == "Quick"][0]["overlays"], [])

    def test_a_punch_becomes_a_marker_and_a_snap_in_on_a_slow_push(self):
        spec, _, _ = self.build()
        cold = spec["clips"][0]
        self.assertEqual([m["at"] for m in cold["markers"] if m["name"] == "punch"], [118.0])
        zooms = {k["at"]: k["zoom"] for k in cold["camera"]}
        self.assertGreater(zooms[118.0], max(z for at, z in zooms.items() if at < 117.5))
        self.assertEqual(cold["camera"][0]["zoom"], 1.0)                 # life from the first frame

    def test_a_long_talking_clip_has_camera_life_inside_the_gap(self):
        spec, tl, _ = self.build(cold_open={"title": "c", "whoosh": False, "clips": [{"src": "a", "in": 0.0, "out": 40.0}]})
        keys = [k["at"] for k in spec["clips"][0]["camera"]]
        gaps = [b - a for a, b in zip(keys, keys[1:])]
        self.assertLessEqual(max(gaps), E.PROFILES["restrained"]["max_gap"])
        self.assertLessEqual(len(keys), E.MAX_CAMERA_KEYS)
        self.assertEqual(keys, sorted(keys))

    def test_ambient_can_be_turned_off_for_a_clip(self):
        spec, _, _ = self.build(cold_open={"title": "c", "whoosh": False,
                                           "clips": [{"src": "a", "in": 0.0, "out": 5.0, "ambient": False}]})
        self.assertEqual(spec["clips"][0]["camera"], [])

    def test_the_whole_episode_passes_its_own_pacing_gate(self):
        _, tl, _ = self.build()
        self.assertTrue(T.pacing(tl)["ok"], T.pacing(tl)["issues"])
        self.assertEqual(tl["warnings"], [])

    def test_a_kill_clip_gets_the_montage_treatment(self):
        spec, tl, _ = self.build()
        rec = next(c for c in spec["clips"] if c.get("label") == "Receipts")
        self.assertEqual([m["name"] for m in rec["markers"]], ["kill"])
        self.assertTrue(any(o["kind"] == "callout" and o["text"] == "120 M SHOT" for o in rec["overlays"]))
        self.assertTrue(any(s["on"]["clip"] == rec["id"] and s["on"].get("at") == 906.0 for s in spec["tracks"]["sfx"]))
        self.assertTrue(rec["camera"])

    def test_a_chapter_music_bed_spans_exactly_that_chapter(self):
        spec, tl, _ = self.build()
        beds = [m for m in tl["tracks"]["music"] if m["id"] == "bed1"]
        rec = next(c for c in tl["clips"] if c["label"] == "Receipts")
        self.assertEqual(len(beds), 1)
        self.assertAlmostEqual(beds[0]["t0"], rec["start"], places=2)
        self.assertAlmostEqual(beds[0]["t1"], rec["end"], places=2)
        end = next(m for m in tl["tracks"]["music"] if m["id"] == "endbed")
        self.assertAlmostEqual(end["t1"], tl["duration"], places=2)

    def test_the_title_is_a_placeholder_until_the_author_has_one(self):
        spec, _, facts = self.build()
        self.assertIn("not chosen yet", spec["notes"])
        self.assertIsNone(facts["title"])
        spec2, _, facts2 = self.build(title="A Name")
        self.assertIn("A Name", spec2["notes"])
        self.assertNotIn("A NAME", [o["text"] for c in spec2["clips"] for o in c["overlays"]])   # never baked into the picture

    def test_facts_describe_what_the_edit_contains(self):
        _, _, facts = self.build()
        self.assertEqual(facts["clips"], 5)
        self.assertEqual([c["title"] for c in facts["chapters"]], ["Cold open", "Mortars", "Receipts", "Dance"])

    def test_the_sting_and_end_cards_name_the_series_and_number(self):
        spec, _, _ = self.build()
        sting = next(c for c in spec["clips"] if c["id"] == "sting")
        self.assertEqual([o["text"] for o in sting["overlays"]], ["TECK MISH MASH", "#1"])
        self.assertTrue(any(s["id"] == "sting" for s in spec["tracks"]["sfx"]))

    def test_the_sting_can_carry_its_own_theme_so_the_card_is_not_silent(self):
        spec, tl, _ = self.build(sting={"dur": 2.6, "music": {"asset": "mus_ff58ea86d9d6dddb", "gain_db": -12.0}})
        bed = next(m for m in tl["tracks"]["music"] if m["id"] == "stingbed")
        card = next(c for c in tl["clips"] if c["id"] == "sting")
        self.assertAlmostEqual(bed["t0"], card["start"], places=2)
        self.assertAlmostEqual(bed["t1"], card["end"], places=2)
        self.assertFalse(bed["duck"]["enabled"])

    def test_no_clips_is_an_error(self):
        with self.assertRaises(E.SpecError):
            TPL.episode(lineup(cold_open=None, chapters=[]))


class TestKillPartsAreSharedWithTheMontage(unittest.TestCase):
    def test_the_montage_keeps_one_callout_per_kill_marker(self):
        spec = E.normalize(build())
        tagged = [c for c in spec["clips"] if c["type"] == "clip" and c["markers"]]
        self.assertTrue(tagged)
        for c in tagged:
            self.assertEqual({m["name"] for m in c["markers"]}, {"kill"})
            self.assertEqual(len(c["markers"]), len([o for o in c["overlays"] if o["kind"] == "callout"]))


class TestEpisodeBrand(unittest.TestCase):
    BRAND = {"accent": "#35D0FF", "label_style": "brand_label", "title_style": "brand_title"}

    def build(self, **kw):
        return E.normalize(TPL.episode(lineup(brand=self.BRAND, **kw)))

    def test_lower_thirds_use_the_brand_typeface_and_accent(self):
        spec = self.build()
        first = next(c for c in spec["clips"] if c.get("label") == "Mortars")
        labels = [o for o in first["overlays"] if o["text"] in ("MORTARS", "ACT 1")]
        self.assertTrue(labels)
        for o in labels:
            self.assertEqual((o["style"], o["accent"]), ("brand_label", "#35D0FF"))

    def test_the_sting_and_end_cards_use_the_title_face_and_the_accent_number(self):
        spec = self.build()
        for cid in ("sting", "end"):
            card = next(c for c in spec["clips"] if c["id"] == cid)
            self.assertTrue(all(o.get("style") == "brand_title" for o in card["overlays"]))
            self.assertEqual(card["overlays"][1]["color"], "#35D0FF")

    def test_without_a_brand_nothing_changes(self):
        spec = E.normalize(TPL.episode(lineup()))
        for c in spec["clips"]:
            for o in c["overlays"]:
                self.assertNotIn("style", o)

    def test_an_episode_turns_the_glue_compressor_on_unless_told_not_to(self):
        self.assertTrue(E.normalize(TPL.episode(lineup()))["master"]["glue"])
        self.assertFalse(E.normalize(TPL.episode(lineup(glue=False)))["master"]["glue"])
        self.assertFalse(E.normalize({"name": "x", "sources": {"v": {"file": "D:/a.mp4"}},
                                      "clips": [{"id": "a", "src": "v", "in": 0, "out": 2}]})["master"]["glue"])


class TestKillContext(unittest.TestCase):
    """The first episode's kill clips had 6 s before the kill and 3 after: the kill without the fight."""

    def lineup(self, **clip):
        c = dict({"src": "v1", "in": 100.0, "out": 130.0, "kills": [{"t": 115.0}]}, **clip)
        return {"sources": SOURCES, "chapters": [{"title": "The Receipts", "clips": [c]}]}

    def test_a_kill_with_room_either_side_passes(self):
        self.assertEqual(TPL.context_issues(self.lineup()), [])

    def test_six_seconds_of_lead_up_is_flagged(self):
        issues = TPL.context_issues(self.lineup(**{"in": 109.0, "out": 130.0}))
        self.assertEqual(len(issues), 1)
        self.assertIn("6.0 s before the first kill", issues[0])
        self.assertNotIn("after the last", issues[0])

    def test_three_seconds_of_aftermath_is_flagged(self):
        issues = TPL.context_issues(self.lineup(out=118.0))
        self.assertIn("3.0 s after the last", issues[0])

    def test_both_short_is_one_line_naming_both(self):
        (issue,) = TPL.context_issues(self.lineup(**{"in": 109.0, "out": 118.0}))
        self.assertIn("before the first kill", issue)
        self.assertIn("after the last", issue)
        self.assertIn("The Receipts", issue)

    def test_a_deliberate_cutaway_is_exempt(self):
        self.assertEqual(TPL.context_issues(self.lineup(**{"in": 112.0, "out": 118.0, "cutaway": True})), [])

    def test_a_clip_with_no_kills_is_not_judged_here(self):
        self.assertEqual(TPL.context_issues(self.lineup(kills=[], **{"in": 112.0, "out": 118.0})), [])

    def test_the_cold_open_is_checked_too(self):
        lu = {"sources": SOURCES, "cold_open": {"title": "x", "clips": [{"src": "v1", "in": 100.0, "out": 105.0, "kills": [{"t": 103.0}]}]}}
        self.assertEqual(len(TPL.context_issues(lu)), 1)

    def test_the_measure_runs_from_the_first_and_last_of_several_kills(self):
        c = {"src": "v1", "in": 100.0, "out": 140.0, "kills": [{"t": 112.0}, {"t": 125.0}, {"t": 136.0}]}
        lu = {"sources": SOURCES, "chapters": [{"title": "t", "clips": [c]}]}
        (issue,) = TPL.context_issues(lu)
        self.assertIn("4.0 s after the last", issue)
        self.assertNotIn("before", issue)


class TestExpandMoments(unittest.TestCase):
    doc = {"arcs": [{"id": "m004", "start": 3005.6, "end": 3062.0, "first": 3020.0, "last": 3055.0, "kills": [
        {"id": "k001", "t": 3020.0, "dist_m": 68.0, "weapon": "gun", "headshot": False, "through_cover": False},
        {"id": "k002", "t": 3055.0, "dist_m": 49.0, "weapon": "grenade", "headshot": False, "through_cover": False}]}]}

    def lineup(self, **clip):
        c = dict({"src": "v1", "moment": "m004"}, **clip)
        return {"sources": SOURCES, "chapters": [{"title": "The Receipts", "clips": [c]}]}

    def expand(self, lu, doc=None, kills=None):
        return TPL.expand_moments(lu, lambda alias: doc if doc is not None else self.doc, lambda alias: kills or {})

    def test_the_arc_becomes_the_clip_with_its_kills(self):
        lu = self.lineup()
        self.expand(lu)
        c = lu["chapters"][0]["clips"][0]
        self.assertEqual((c["in"], c["out"]), (3005.6, 3062.0))
        self.assertEqual([k["t"] for k in c["kills"]], [3020.0, 3055.0])
        self.assertEqual(c["kills"][0]["src"], "v1")
        self.assertEqual(c["snap"], "words")
        self.assertEqual(c["moment"], "m004")

    def test_the_icon_comes_from_the_kill_feed_when_it_has_one(self):
        lu = self.lineup()
        self.expand(lu, kills={"k001": {"icon": "D:/w/killfeed_icons/k001.png", "victim": "Yutpi"}})
        k = lu["chapters"][0]["clips"][0]["kills"][0]
        self.assertEqual((k["icon"], k["victim"]), ("D:/w/killfeed_icons/k001.png", "Yutpi"))

    def test_trim_takes_seconds_off_the_lead_up_and_the_aftermath_and_drops_kills_outside(self):
        lu = self.lineup(trim=[5.0, 12.0])
        self.expand(lu)
        c = lu["chapters"][0]["clips"][0]
        self.assertEqual((c["in"], c["out"]), (3010.6, 3050.0))
        self.assertEqual([k["t"] for k in c["kills"]], [3020.0])

    def test_the_recorded_first_kill_guards_against_ids_that_moved(self):
        self.expand(self.lineup(at=3020.0))                       # matches: fine
        with self.assertRaises(E.SpecError) as ctx:
            self.expand(self.lineup(at=2800.0))
        self.assertIn("ids shifted", str(ctx.exception))

    def test_a_trim_that_leaves_nothing_is_an_error(self):
        with self.assertRaises(E.SpecError):
            self.expand(self.lineup(trim=[30.0, 30.0]))

    def test_an_unknown_moment_or_source_is_named(self):
        with self.assertRaises(E.SpecError) as ctx:
            self.expand(self.lineup(moment="m999"))
        self.assertIn("m999", str(ctx.exception))
        with self.assertRaises(E.SpecError) as ctx2:
            TPL.expand_moments(self.lineup(), lambda a: None, lambda a: {})
        self.assertIn("no moments.json", str(ctx2.exception))

    def test_clips_without_a_moment_are_untouched_and_the_expanded_lineup_passes_the_context_check(self):
        lu = self.lineup()
        lu["chapters"][0]["clips"].append({"src": "v1", "in": 5.0, "out": 9.0})
        done = self.expand(lu)
        self.assertEqual(len(done), 1)
        self.assertEqual(lu["chapters"][0]["clips"][1], {"src": "v1", "in": 5.0, "out": 9.0})
        self.assertEqual(TPL.context_issues(lu), [])

    def test_the_expanded_lineup_builds_an_episode(self):
        lu = {"name": "e", "sources": SOURCES, "profile": "restrained",
              "chapters": [{"title": "The Receipts", "clips": [{"src": "v1", "moment": "m004"}]}]}
        self.expand(lu)
        spec = E.normalize(TPL.episode(lu))
        clip = next(c for c in spec["clips"] if c["type"] == "clip")
        self.assertEqual(len([m for m in clip["markers"] if m["name"] == "kill"]), 2)


if __name__ == "__main__":
    unittest.main()
