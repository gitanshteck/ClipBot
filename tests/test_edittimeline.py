"""Tests for clipbot/edittimeline.py: retiming math, the compiled timeline, the pacing gate."""

import unittest

from clipbot import editspec as E
from clipbot import edittimeline as T

SOURCES = {"v": {"file": "D:/x.mp4"}, "w": {"file": "D:/y.mp4"}}
RAMP = [{"at": 12, "rate": 1.0, "ease": "inout2"}, {"at": 13, "rate": 0.5}, {"at": 15, "rate": 0.5}, {"at": 16, "rate": 1.0}]


def spec(clips, **extra):
    d = {"name": "t", "sources": SOURCES, "clips": clips}
    d.update(extra)
    return E.normalize(d)


def clip(**kw):
    c = {"id": "a", "src": "v", "in": 10, "out": 20}
    c.update(kw)
    return c


class TestPieces(unittest.TestCase):
    def test_plain_clip_is_one_piece(self):
        c = spec([clip()])["clips"][0]
        pieces = T.build_pieces(c)
        self.assertEqual(len(pieces), 1)
        self.assertTrue(T.is_plain(pieces))
        self.assertAlmostEqual(pieces[0]["out_dur"], 10.0)

    def test_constant_slowmo_stretches_the_output(self):
        c = spec([clip(speed=[{"at": 10, "rate": 0.5}, {"at": 20, "rate": 0.5}])])["clips"][0]
        pieces = T.build_pieces(c)
        self.assertAlmostEqual(sum(p["out_dur"] for p in pieces), 20.0, places=3)

    def test_pieces_tile_the_source_with_no_gap_or_overlap(self):
        c = spec([clip(speed=RAMP)])["clips"][0]
        pieces = T.build_pieces(c)
        self.assertAlmostEqual(pieces[0]["s0"], 10.0)
        self.assertAlmostEqual(pieces[-1]["s1"], 20.0)
        for a, b in zip(pieces, pieces[1:]):
            self.assertAlmostEqual(a["s1"], b["s0"], places=6)

    def test_ramp_makes_the_output_longer_than_the_source(self):
        c = spec([clip(speed=RAMP)])["clips"][0]
        pieces = T.build_pieces(c)
        self.assertGreater(sum(p["out_dur"] for p in pieces), 10.5)
        self.assertFalse(T.is_plain(pieces))

    def test_ramp_rates_stay_between_the_keys(self):
        c = spec([clip(speed=RAMP)])["clips"][0]
        for p in T.build_pieces(c):
            self.assertGreaterEqual(p["rate"], 0.5 - 1e-6)
            self.assertLessEqual(p["rate"], 1.0 + 1e-6)

    def test_freeze_adds_its_duration(self):
        c = spec([clip(freezes=[{"at": 15, "dur": 1.0}])])["clips"][0]
        pieces = T.build_pieces(c)
        self.assertEqual([p["kind"] for p in pieces], ["play", "freeze", "play"])
        self.assertAlmostEqual(sum(p["out_dur"] for p in pieces), 11.0)


class TestSourceToOutput(unittest.TestCase):
    def test_monotonic_and_exact_at_the_ends(self):
        pieces = T.build_pieces(spec([clip(speed=RAMP)])["clips"][0])
        times = [10 + i * 0.25 for i in range(41)]
        mapped = [T.src_to_out(pieces, t) for t in times]
        self.assertAlmostEqual(mapped[0], 0.0)
        self.assertAlmostEqual(mapped[-1], sum(p["out_dur"] for p in pieces), places=6)
        self.assertEqual(mapped, sorted(mapped))

    def test_unretimed_part_maps_one_to_one(self):
        pieces = T.build_pieces(spec([clip(speed=RAMP)])["clips"][0])
        self.assertAlmostEqual(T.src_to_out(pieces, 12.0), 2.0)

    def test_slow_part_takes_twice_as_long(self):
        pieces = T.build_pieces(spec([clip(speed=RAMP)])["clips"][0])
        self.assertAlmostEqual(T.src_to_out(pieces, 15.0) - T.src_to_out(pieces, 13.0), 4.0, places=3)


class TestTimeline(unittest.TestCase):
    def test_cuts_add_up(self):
        tl = T.compile_timeline(spec([clip(), clip(id="b", src="w", **{"in": 0, "out": 5})]))
        self.assertAlmostEqual(tl["duration"], 15.0)
        self.assertAlmostEqual(tl["clips"][1]["start"], 10.0)

    def test_crossfade_overlaps_both_clips(self):
        tl = T.compile_timeline(spec([clip(), clip(id="b", src="w", transition_in={"type": "xfade", "dur": 0.5},
                                                  **{"in": 0, "out": 10})]))
        self.assertAlmostEqual(tl["duration"], 19.5)
        self.assertAlmostEqual(tl["clips"][1]["start"], 9.5)
        self.assertAlmostEqual(tl["clips"][1]["overlap_in"], 0.5)

    def test_overlap_is_capped_at_half_a_clip(self):
        tl = T.compile_timeline(spec([clip(), clip(id="b", src="w", transition_in={"type": "xfade", "dur": 3.0},
                                                  **{"in": 0, "out": 1})]))
        self.assertLessEqual(tl["clips"][1]["overlap_in"], 0.5 + 1e-6)

    def test_durations_land_on_whole_frames(self):
        tl = T.compile_timeline(spec([clip(**{"in": 10, "out": 10.4})]))
        e = tl["clips"][0]
        self.assertAlmostEqual(e["dur"] * 60, round(e["dur"] * 60), places=6)

    def test_marker_and_overlay_follow_a_ramp(self):
        tl = T.compile_timeline(spec([clip(speed=RAMP, markers=[{"at": 15, "name": "kill"}],
                                           overlays=[{"kind": "callout", "at": 15, "dur": 2.0, "text": "X"}])]))
        e = tl["clips"][0]
        self.assertGreater(e["markers"][0]["at"], 5.0)            # not 5.0: the ramp slowed what came before it
        self.assertAlmostEqual(e["overlays"][0]["t0"], e["markers"][0]["at"])

    def test_sfx_anchored_to_a_source_time(self):
        tl = T.compile_timeline(spec(
            [clip(), clip(id="b", src="w", **{"in": 0, "out": 5})],
            tracks={"sfx": [{"id": "s", "asset": "sfx_08a073fd8c00c828", "on": {"clip": "b", "at": 2.0}}]}))
        self.assertAlmostEqual(tl["tracks"]["sfx"][0]["hit"], 12.0)

    def test_sfx_follows_its_clip_when_an_earlier_clip_changes(self):
        def hit(first_out):
            tl = T.compile_timeline(spec(
                [clip(**{"out": first_out}), clip(id="b", src="w", **{"in": 0, "out": 5})],
                tracks={"sfx": [{"id": "s", "asset": "sfx_08a073fd8c00c828", "on": {"clip": "b", "at": 2.0}}]}))
            return tl["tracks"]["sfx"][0]["hit"]
        self.assertAlmostEqual(hit(20) - hit(15), 5.0)

    def test_sfx_past_the_end_is_dropped_with_a_warning(self):
        tl = T.compile_timeline(spec(
            [clip(), clip(id="b", src="w", **{"in": 0, "out": 5})],
            tracks={"sfx": [{"id": "s", "asset": "sfx_08a073fd8c00c828", "on": {"clip": "b", "at": 2.0, "offset": 60}}]}))
        self.assertEqual(tl["tracks"]["sfx"], [])
        self.assertTrue(any("outside the video" in w for w in tl["warnings"]))

    def test_music_is_clamped_to_the_edit(self):
        tl = T.compile_timeline(spec([clip()], tracks={"music": [{"id": "m", "asset": "mus_3a25c534c578d312"}]}))
        m = tl["tracks"]["music"][0]
        self.assertEqual((m["t0"], m["t1"]), (0.0, tl["duration"]))

    def test_events_are_sorted(self):
        tl = T.compile_timeline(spec([clip(speed=RAMP, markers=[{"at": 15, "name": "kill"}],
                                           fx=[{"type": "flash", "at": 15, "dur": 0.1}])]))
        times = [e["t"] for e in tl["events"]]
        self.assertEqual(times, sorted(times))


class TestPacing(unittest.TestCase):
    def test_raw_gameplay_fails_the_gate(self):
        rep = T.pacing(T.compile_timeline(spec([clip()])))
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["dead"][0]["length"], 10.0)

    def test_a_move_every_few_seconds_passes(self):
        cam = [{"at": 10 + i * 3, "zoom": 1.0 + 0.02 * (i % 2)} for i in range(4)]
        rep = T.pacing(T.compile_timeline(spec([clip(camera=cam, overlays=[{"kind": "text", "at": 10, "dur": 2.0, "text": "X"}])])))
        self.assertEqual(rep["dead"], [])

    def test_the_profile_sets_the_gap(self):
        raw = spec([clip(**{"out": 14})])
        self.assertEqual(T.pacing(T.compile_timeline(raw), max_gap=20.0)["dead"], [])

    def test_flicker_and_hold_overlays_are_flagged(self):
        flick = spec([clip(overlays=[{"kind": "text", "at": 12, "dur": 0.6, "text": "X"}])])
        hold = spec([clip(overlays=[{"kind": "text", "at": 12, "dur": 6.0, "text": "X"}])])
        self.assertTrue(any(i["what"] == "dwell" for i in T.pacing(T.compile_timeline(flick))["issues"]))
        self.assertTrue(any(i["what"] == "dwell" for i in T.pacing(T.compile_timeline(hold))["issues"]))


class TestChapters(unittest.TestCase):
    def test_cards_and_tagged_clips_are_chapters(self):
        tl = T.compile_timeline(spec([
            {"id": "i", "type": "card", "dur": 12.0},
            clip(id="a", tags=["chapter"], label="Headshots", **{"out": 30}),
            clip(id="b", src="w", **{"in": 0, "out": 20}),
            clip(id="c", src="w", tags=["chapter"], label="Nades", **{"in": 30, "out": 50}),
        ]))
        ch = T.chapters(tl)
        self.assertEqual([c["title"] for c in ch], ["i", "Headshots", "Nades"])
        self.assertEqual(ch[0]["t"], 0.0)

    def test_chapters_closer_than_ten_seconds_are_merged(self):
        tl = T.compile_timeline(spec([
            clip(id="a", tags=["chapter"], label="One", **{"out": 15}),
            clip(id="b", src="w", tags=["chapter"], label="Two", **{"in": 0, "out": 5}),
        ]))
        ch = T.chapters(tl)
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["title"], "One + Two")

    def test_first_chapter_is_forced_to_zero(self):
        tl = T.compile_timeline(spec([clip(id="a"), clip(id="b", src="w", tags=["chapter"], label="Later", **{"in": 0, "out": 20})]))
        ch = T.chapters(tl)
        self.assertEqual(ch[0], {"t": 0.0, "title": "Intro"})


if __name__ == "__main__":
    unittest.main()
