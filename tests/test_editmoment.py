"""Tests for clipbot/editmoment.py: arcs from kills, ranking, coverage, and the evidence sheet.

The arc rules are what the episode's missing-context complaint was about (a kill with six seconds before
it and three after), so each is pinned: the lead-up and aftermath, a multi-kill being one arc, windows that
touch being merged, and an over-long run being split at its biggest gap.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from clipbot import editmoment as M
from clipbot.config import Settings
from clipbot.workspace import Workspace


def kill(t, **kw):
    return dict({"id": "k{0}".format(int(t)), "t": float(t), "dist_m": 30.0, "weapon": "gun", "headshot": False,
                 "through_cover": False, "victim": "", "source": "feed"}, **kw)


class TestArcs(unittest.TestCase):
    def test_a_lone_kill_gets_its_lead_up_and_its_aftermath(self):
        (a,) = M.arcs_from_kills([kill(100)], 1000.0)
        self.assertEqual((a["start"], a["end"]), (100 - M.LEAD, 100 + M.TAIL))
        self.assertEqual(M.context(a), {"lead": M.LEAD, "tail": M.TAIL, "length": M.LEAD + M.TAIL})

    def test_kills_close_together_are_one_arc(self):
        (a,) = M.arcs_from_kills([kill(100), kill(108), kill(120)], 1000.0)
        self.assertEqual((a["first"], a["last"], len(a["kills"])), (100.0, 120.0, 3))
        self.assertEqual((a["start"], a["end"]), (100 - M.LEAD, 120 + M.TAIL))

    def test_kills_far_apart_are_separate_arcs(self):
        arcs = M.arcs_from_kills([kill(100), kill(300)], 1000.0)
        self.assertEqual([len(a["kills"]) for a in arcs], [1, 1])
        self.assertEqual([a["id"] for a in arcs], ["m001", "m002"])

    def test_windows_that_touch_are_merged_so_no_footage_is_cut_twice(self):
        # 24 s apart: further than MERGE_GAP, but one's aftermath reaches into the other's lead-up
        arcs = M.arcs_from_kills([kill(100), kill(124)], 1000.0, lead=14.0, tail=12.0)
        self.assertEqual(len(arcs), 1)
        self.assertEqual(len(arcs[0]["kills"]), 2)
        self.assertEqual((arcs[0]["start"], arcs[0]["end"]), (86.0, 136.0))

    def test_the_window_is_clamped_to_the_video(self):
        (a,) = M.arcs_from_kills([kill(5), ], 30.0)
        self.assertEqual(a["start"], 0.0)
        (b,) = M.arcs_from_kills([kill(25)], 30.0)
        self.assertEqual(b["end"], 30.0)

    def test_an_arc_too_long_is_split_at_its_largest_gap(self):
        ks = [kill(t) for t in (0, 14, 28, 42, 56, 70, 84, 98, 130, 144)]       # every 14 s, then a 32 s gap
        arcs = M.arcs_from_kills(ks, 1000.0, merge_gap=40.0, max_arc=100.0)
        self.assertGreater(len(arcs), 1)
        self.assertTrue(all(a["end"] - a["start"] <= 100.0 + 1e-6 for a in arcs), [(a["start"], a["end"]) for a in arcs])
        self.assertEqual(sum(len(a["kills"]) for a in arcs), 10)
        self.assertTrue(all(b["start"] >= a["end"] - 1e-6 for a, b in zip(arcs, arcs[1:])), "split arcs must not share footage")

    def test_no_kills_no_arcs(self):
        self.assertEqual(M.arcs_from_kills([], 100.0), [])

    def test_unsorted_input_is_handled(self):
        arcs = M.arcs_from_kills([kill(300), kill(100)], 1000.0)
        self.assertEqual([a["first"] for a in arcs], [100.0, 300.0])

    def test_a_kill_with_no_weapon_info_is_still_a_kill(self):
        (a,) = M.arcs_from_kills([{"id": "c001", "t": 50.0, "source": "confirm"}], 100.0)
        self.assertEqual(a["kills"][0]["weapon"], "unknown")
        self.assertEqual(a["kills"][0]["source"], "confirm")


class TestScore(unittest.TestCase):
    def arc(self, *kills, start=0.0, end=60.0):
        return {"id": "m001", "start": start, "end": end, "first": kills[0]["t"], "last": kills[-1]["t"], "kills": list(kills)}

    def test_a_nade_headshot_far_kill_outranks_a_plain_close_one(self):
        big = M.score_arc(self.arc(kill(10, weapon="grenade", headshot=True, dist_m=120.0)))
        plain = M.score_arc(self.arc(kill(10, dist_m=5.0)))
        self.assertGreater(big["score"], plain["score"])

    def test_more_kills_in_one_arc_add_a_multi_bonus(self):
        one = M.score_arc(self.arc(kill(10)))
        three = M.score_arc(self.arc(kill(10), kill(14), kill(18)))
        self.assertEqual(one["parts"]["multi"], 0.0)
        self.assertEqual(three["parts"]["multi"], 2 * M.W_MULTI)

    def test_a_loudness_spike_inside_the_arc_counts_and_one_outside_does_not(self):
        inside = M.score_arc(self.arc(kill(10)), energy=[{"start": 20, "end": 25, "z_score": 3.0}])
        outside = M.score_arc(self.arc(kill(10)), energy=[{"start": 200, "end": 205, "z_score": 9.0}])
        self.assertAlmostEqual(inside["parts"]["reaction"], 3.0 * M.W_REACTION)
        self.assertEqual(outside["parts"]["reaction"], 0.0)

    def test_the_reaction_part_is_capped(self):
        s = M.score_arc(self.arc(kill(10)), energy=[{"start": 20, "end": 25, "z_score": 40.0}])
        self.assertEqual(s["parts"]["reaction"], M.CAP_REACTION)

    def test_a_chat_burst_counts_and_is_capped(self):
        s = M.score_arc(self.arc(kill(10)), chat=[{"start": 0, "end": 30, "message_rate_multiplier": 50.0}])
        self.assertEqual(s["parts"]["chat"], M.CAP_CHAT)

    def test_an_analysis_pick_over_the_same_stretch_is_carried_with_its_words(self):
        cands = [{"start_time": 5.0, "end_time": 30.0, "description": "teck wins a duel", "why": "x"}]
        s = M.score_arc(self.arc(kill(10)), candidates=cands)
        self.assertEqual(s["parts"]["analysis"], M.W_ANALYSIS)
        self.assertEqual(s["analysis"][0]["description"], "teck wins a duel")

    def test_the_total_is_the_sum_of_the_parts(self):
        s = M.score_arc(self.arc(kill(10), kill(14)), energy=[{"start": 0, "end": 60, "z_score": 2.5}])
        self.assertAlmostEqual(s["score"], sum(s["parts"].values()), places=1)


class TestLines(unittest.TestCase):
    doc = {"segments": [{"start": 0, "end": 2, "text": "before"}, {"start": 10, "end": 12, "text": " inside ", "low_confidence": True},
                        {"start": 13, "end": 14, "text": "?"}, {"start": 20, "end": 22, "text": "after"}]}

    def test_only_lines_inside_the_window_and_low_confidence_ones_are_marked_not_dropped(self):
        got = M.lines_between(self.doc, 9.0, 15.0)
        self.assertEqual([(l["t"], l["text"], l["low"]) for l in got], [(10.0, "inside", True)])

    def test_the_limit_is_respected(self):
        doc = {"segments": [{"start": i, "end": i + 1, "text": "x{0}".format(i)} for i in range(30)]}
        self.assertEqual(len(M.lines_between(doc, 0, 100, limit=5)), 5)


class TestFrames(unittest.TestCase):
    def test_frames_cover_the_arc_and_include_every_kill_and_a_look_just_before_the_first(self):
        arc = {"start": 100.0, "end": 160.0, "first": 120.0, "last": 140.0, "kills": [kill(120), kill(140)], "id": "m001"}
        ts = M.frame_times(arc, 12)
        self.assertEqual(ts, sorted(ts))
        self.assertAlmostEqual(ts[0], 100.0)
        self.assertAlmostEqual(ts[-1], 160.0)
        for k in arc["kills"]:
            self.assertTrue(any(abs(t - (k["t"] + 0.4)) < 0.05 for t in ts), k)
        self.assertTrue(any(abs(t - 118.5) < 0.05 for t in ts))
        self.assertTrue(all(b - a >= 0.6 - 1e-9 for a, b in zip(ts, ts[1:])))

    def test_devanagari_is_recognised_so_it_is_kept_off_the_picture(self):
        self.assertTrue(M._devanagari("ये देखो"))
        self.assertFalse(M._devanagari("main nahi chudi"))

    def test_clock_text(self):
        self.assertEqual(M._mmss(75), "1:15")
        self.assertEqual(M._mmss(3725), "1:02:05")


def make_ws(tmp, state=None, killfeed=None):
    ws = Workspace(Path(tmp) / "w")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.write_json(ws.root / "state.json", dict({"duration": 7200.0, "title": "Wardogs stream"}, **(state or {})))
    if killfeed is not None:
        ws.write_json(ws.root / "killfeed.json", killfeed)
    return ws


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_unscanned_stream_says_so(self):
        self.assertEqual(M.coverage(make_ws(self._tmp.name))["verdict"], "not scanned")

    def test_a_feed_only_scan_asks_for_the_popup_pass(self):
        ws = make_ws(self._tmp.name, killfeed={"events": [{"id": "k001", "t": 5.0}]})
        self.assertIn("rescan", M.coverage(ws)["verdict"])

    def test_two_hours_and_nothing_found_is_unread_not_no_kills(self):
        ws = make_ws(self._tmp.name, killfeed={"events": [], "confirm_sightings": 0})
        self.assertEqual(M.coverage(ws)["verdict"], "unread or no kills")

    def test_mostly_popup_only_kills_means_the_feed_was_covered(self):
        ev = [{"id": "c001", "t": 5.0, "source": "confirm"}, {"id": "c002", "t": 50.0, "source": "confirm"}, {"id": "k001", "t": 90.0, "source": "both"}]
        ws = make_ws(self._tmp.name, killfeed={"events": ev, "confirm_sightings": 9})
        row = M.coverage(ws)
        self.assertIn("covered", row["verdict"])
        self.assertEqual(row["sources"], {"both": 1, "feed": 0, "confirm": 2})

    def test_a_clean_scan_is_ok(self):
        ev = [{"id": "k001", "t": 5.0, "source": "both"}, {"id": "k002", "t": 50.0, "source": "both"}]
        ws = make_ws(self._tmp.name, killfeed={"events": ev, "confirm_sightings": 9})
        self.assertEqual(M.coverage(ws)["verdict"], "ok")


class TestBuild(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Settings({"work_root": self._tmp.name}, Path("settings.json"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_writes_moments_json_with_arcs_ranked_context_and_lines(self):
        ev = [{"id": "k001", "t": 100.0, "dist_m": 80.0, "weapon": "gun", "headshot": True, "source": "both"},
              {"id": "k002", "t": 500.0, "dist_m": 8.0, "weapon": "gun", "source": "feed"}]
        ws = make_ws(self._tmp.name, killfeed={"events": ev, "confirm_sightings": 3})
        ws.write_json(ws.transcript_path, {"segments": [{"start": 98, "end": 100, "text": "got him"}]})
        ws.write_json(ws.candidates_path, {"clips": [{"start_time": 95.0, "end_time": 110.0, "description": "a duel", "why": ""}]})
        doc = M.build(ws, self.settings, snap=False)
        self.assertEqual(len(doc["arcs"]), 2)
        first = doc["arcs"][0]
        self.assertEqual(first["context"]["lead"], M.LEAD)
        self.assertEqual(first["lines"][0]["text"], "got him")
        self.assertEqual(first["parts"]["analysis"], M.W_ANALYSIS)
        self.assertEqual(M.ranked(doc)[0]["id"], first["id"])                     # the headshot with the analysis pick wins
        self.assertEqual(M.load(ws)["kills"], 2)
        self.assertEqual(doc["kills_by_source"], {"both": 1, "feed": 1})

    def test_no_killfeed_is_an_error_that_says_what_to_run(self):
        from clipbot.utils import StageError

        with self.assertRaises(StageError) as ctx:
            M.build(make_ws(self._tmp.name), self.settings)
        self.assertIn("killfeed scan", str(ctx.exception))

    def test_the_policy_can_be_set_in_settings_and_by_argument(self):
        ev = [{"id": "k001", "t": 100.0, "source": "feed"}]
        ws = make_ws(self._tmp.name, killfeed={"events": ev})
        s = Settings({"work_root": self._tmp.name, "moments": {"lead": 20, "tail": 9}}, Path("settings.json"))
        a = M.build(ws, s, snap=False)["arcs"][0]
        self.assertEqual((a["context"]["lead"], a["context"]["tail"]), (20.0, 9.0))
        b = M.build(ws, s, lead=5.0, snap=False)["arcs"][0]
        self.assertEqual((b["context"]["lead"], b["context"]["tail"]), (5.0, 9.0))


if __name__ == "__main__":
    unittest.main()
