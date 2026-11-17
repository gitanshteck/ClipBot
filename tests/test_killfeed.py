"""Tests for clipbot/killfeed.py (pure parsing) and clipbot/stages/killfeed.py helpers.

OCR itself is not exercised - it is a Windows API call - but everything after it is
plain data, so the rules learned from reading real feeds are pinned here with
synthetic words: the distance tag as OCR mangles it, a kill versus a death, the
bracket misread as a leading 1, grouping a row seen over several frames, icons.
"""

import unittest
from pathlib import Path

from clipbot import killfeed as K


def word(t, x, y=300, w=60, h=30):
    return {"t": t, "x": x, "y": y, "w": w, "h": h}


class TestNameMatching(unittest.TestCase):
    def setUp(self):
        self.match = K.make_matcher("Teck")

    def test_exact_and_one_edit_are_strong(self):
        for w in ("Teck", "teck", "Teek", "Tick", "Tcck"):
            self.assertEqual(self.match(w), "strong", w)

    def test_a_dissolved_first_or_last_letter_still_matches(self):
        self.assertEqual(self.match("eck"), "strong")
        self.assertEqual(self.match("Tec"), "strong")

    def test_other_names_do_not_match(self):
        for w in ("Lotte", "Kuller", "", "!!", "Tx"):
            self.assertIsNone(self.match(w), w)

    def test_short_names_only_match_exactly(self):
        m = K.make_matcher("Ace")
        self.assertEqual(m("ace"), "strong")
        self.assertIsNone(m("ice"))

    def test_too_short_a_name_is_refused(self):
        with self.assertRaises(ValueError):
            K.make_matcher("A")


class TestDistance(unittest.TestCase):
    def find(self, *texts):
        return K.find_distance([word(t, 100 + 80 * i) for i, t in enumerate(texts)])

    def test_plain_and_bracketed_tags(self):
        self.assertEqual(self.find("[17m]")[2], 17.0)
        self.assertEqual(self.find("(48m)")[2], 48.0)
        self.assertEqual(self.find("[55", "m]")[2], 55.0)

    def test_trailing_bracket_misread_as_a_letter(self):
        self.assertEqual(self.find("[48mJ")[2], 48.0)

    def test_kilometres(self):
        self.assertEqual(self.find("[1.2km]")[2], 1200.0)

    def test_a_name_with_digits_is_not_a_distance(self):
        self.assertIsNone(self.find("Player12", "alpha"))

    def test_indices_point_at_the_tag(self):
        i0, i1, _ = self.find("skull", "[55", "m]", "victim")
        self.assertEqual((i0, i1), (1, 2))


class TestRows(unittest.TestCase):
    def test_words_group_by_height_and_sort_left_to_right(self):
        rows = K.rows_of([word("b", 200, 300), word("a", 100, 302), word("c", 100, 400)])
        self.assertEqual(len(rows), 2)
        self.assertEqual([w["t"] for w in rows[0]["w"]], ["a", "b"])

    def test_readable_name(self):
        self.assertEqual(K.readable_name("KULLER"), "KULLER")
        self.assertEqual(K.readable_name("biunmm"), "biunmm")
        self.assertEqual(K.readable_name("岩石"), "")
        self.assertEqual(K.readable_name("a"), "")
        self.assertEqual(K.readable_name("[bad"), "")

    def test_ocr_noise_seen_in_real_feeds_is_blanked(self):
        for noise in ("(HUNT)MMRÅ+O", "r--r", "ExeCt;TtoNetZetÆ", "[HUNTIMMR*+C", "et4m1 500", "ü.*.ääh"):
            self.assertEqual(K.readable_name(noise), "", noise)
        for name in ("Devil in me", "ALwayXX", "[LADB] JoeyCasino", "sortyFSao", "Khanny"):
            self.assertEqual(K.readable_name(name), name, name)


class TestObserve(unittest.TestCase):
    def setUp(self):
        self.match = K.make_matcher("Teck")

    def kills(self, words):
        return K.observe(words, 10.0, "f.png", self.match)

    def test_a_kill_has_the_player_first_and_a_distance(self):
        out = self.kills([word("Teck", 28), word("[17m]", 300), word("KULLER", 400)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["dist"], 17.0)
        self.assertEqual(out[0]["victim"], "KULLER")
        self.assertTrue(out[0]["strong"])

    def test_a_death_has_the_player_after_the_tag(self):
        self.assertEqual(self.kills([word("KULLER", 28), word("[17m]", 300), word("Teck", 400)]), [])

    def test_a_kill_by_somebody_else_is_ignored(self):
        self.assertEqual(self.kills([word("Lotte", 28), word("[9m]", 300), word("KH", 400)]), [])

    def test_the_player_far_from_the_left_margin_is_not_the_killer(self):
        self.assertEqual(self.kills([word("Teck", 600), word("[17m]", 700), word("KULLER", 800)]), [])

    def test_a_row_with_no_distance_needs_a_readable_victim(self):
        self.assertEqual(self.kills([word("Teck", 28), word("x", 300)]), [])
        self.assertEqual(len(self.kills([word("Teck", 28), word("KULLER", 300)])), 1)

    def test_the_label_strip_at_the_top_is_not_feed(self):
        self.assertEqual(self.kills([word("Teck", 28, y=20), word("[17m]", 300, y=20), word("KULLER", 400, y=20)]), [])


class TestGrouping(unittest.TestCase):
    def obs(self, t, dist, victim="V"):
        return {"t": t, "dist": dist, "victim": victim}

    def test_one_row_seen_over_several_frames_is_one_run(self):
        runs = K.group_runs([self.obs(10, 17), self.obs(11, 17), self.obs(12, 17)])
        self.assertEqual(len(runs), 1)

    def test_different_distances_are_different_rows(self):
        runs = K.group_runs([self.obs(10, 17), self.obs(11, 40)])
        self.assertEqual(len(runs), 2)

    def test_a_long_gap_starts_a_new_run(self):
        runs = K.group_runs([self.obs(10, 17), self.obs(30, 17)])
        self.assertEqual(len(runs), 2)

    def test_the_modal_distance_beats_a_single_misread(self):
        run = [self.obs(10, 55), self.obs(11, 55), self.obs(12, 155), self.obs(13, 55)]
        self.assertEqual(K.modal_distance(run), 55.0)
        self.assertIsNone(K.modal_distance([self.obs(10, None)]))

    def test_a_bracket_read_as_a_leading_one_is_dropped(self):
        events = [{"dist_m": 110.0, "first": 100.0, "n_obs": 1}, {"dist_m": 10.0, "first": 101.0, "n_obs": 4}]
        out = K.drop_prefixed_twins(events)
        self.assertEqual([e["dist_m"] for e in out], [10.0])

    def test_a_real_far_kill_is_kept(self):
        events = [{"dist_m": 110.0, "first": 100.0, "n_obs": 3}, {"dist_m": 10.0, "first": 300.0, "n_obs": 4}]
        self.assertEqual(len(K.drop_prefixed_twins(events)), 2)


class TestIcons(unittest.TestCase):
    GUN = (28, 132, 5, 37, 840)

    def test_a_rifle_is_a_gun(self):
        self.assertEqual(K.classify_icons([self.GUN]), ("gun", False, False))

    def test_a_skull_makes_it_a_headshot(self):
        self.assertEqual(K.classify_icons([self.GUN, (176, 216, 0, 42, 992)]), ("gun", True, False))

    def test_a_hollow_modifier_is_through_cover(self):
        self.assertEqual(K.classify_icons([self.GUN, (171, 215, 0, 44, 570)])[2], True)

    def test_a_round_icon_is_a_grenade(self):
        self.assertEqual(K.classify_icons([(30, 70, 5, 45, 600)])[0], "grenade")

    def test_specks_and_nothing(self):
        self.assertEqual(K.classify_icons([]), ("unknown", False, False))
        self.assertEqual(K.classify_icons([(1, 9, 0, 10, 50)]), ("unknown", False, False))

    def test_icon_extent_stops_before_the_distance_digits(self):
        # weapon, then a short blob (digit) - not part of the icon
        self.assertEqual(K.icon_extent([self.GUN, (201, 222, 9, 32, 132)], margin=6), 138)
        # weapon, a full-height skull, then digits
        self.assertEqual(K.icon_extent([self.GUN, (171, 215, 0, 44, 570), (232, 238, 4, 36, 37), (246, 257, 10, 33, 133)], margin=6), 221)
        self.assertIsNone(K.icon_extent([]))

    def test_blobs_found_in_a_synthetic_row(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (260, 50), (20, 20, 20))
        d = ImageDraw.Draw(im)
        d.rectangle((10, 12, 110, 36), fill=(255, 255, 255))      # weapon
        d.rectangle((150, 4, 190, 46), fill=(255, 255, 255))      # skull-sized modifier
        blobs = K.icon_blobs(im)
        self.assertEqual(len(blobs), 2)
        self.assertEqual((blobs[0][0], blobs[0][1]), (10, 110))
        self.assertEqual(K.classify_icons(blobs)[0], "gun")


class TestStageHelpers(unittest.TestCase):
    def test_chunks_cover_the_video_without_a_sliver(self):
        from clipbot.stages import killfeed as stage
        chunks = stage._chunks(4000.0, 1800.0)
        self.assertEqual(chunks[0], (0.0, 1800.0))
        self.assertEqual(sum(c[1] for c in chunks), 4000.0)
        self.assertEqual(stage._chunks(0.3, 1800.0), [])


def words(*texts):
    """OCR words as the Windows OCR driver returns them: one dict per token."""
    return [{"t": t, "x": 10 * i, "y": 5, "w": 8, "h": 12} for i, t in enumerate(" ".join(texts).split())]


class TestConfirmPopup(unittest.TestCase):
    """The `KILL CONFIRMED +$2,750 250XP` popup at the crosshair: the record of a kill that survives a webcam over the feed."""

    # the four OCR readings of the same popup measured on a real clip (frames 4, 5, 7, 6)
    REAL = [("2 � 750", "KILL ONFJRMEO .S2.7S0"), ("+ 2.750", "KILL CONFIRMEO"), ("KILL CONFIRMED 250XP",)]

    def test_the_real_misreadings_are_all_the_popup(self):
        for texts in self.REAL:
            self.assertIsNotNone(K.parse_confirm(words(*texts)), texts)

    def test_the_xp_tag_is_read(self):
        self.assertEqual(K.parse_confirm(words("KILL CONFIRMED 250XP"))["xp"], 250)

    def test_nothing_on_screen_is_not_a_kill(self):
        self.assertIsNone(K.parse_confirm([]))
        self.assertIsNone(K.parse_confirm(words("HEALTH 100 AMMO 30")))

    def test_a_shop_that_says_confirm_is_not_a_kill(self):
        self.assertIsNone(K.parse_confirm(words("CONFIRM PURCHASE $500")))

    def test_confirmed_with_a_kill_word_but_no_other_text_is_a_kill(self):
        self.assertIsNotNone(K.parse_confirm(words("KILL", "CONFIRMED")))

    def test_the_popup_on_four_frames_in_five_is_one_run(self):
        obs = [{"t": 3.0, "amount": None, "xp": None}, {"t": 4.0, "amount": None, "xp": None}, {"t": 6.0, "amount": None, "xp": 250}]
        runs = K.group_confirms(obs)
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["first"], runs[0]["last"], runs[0]["n_obs"], runs[0]["xp"]), (3.0, 6.0, 3, 250))

    def test_two_kills_well_apart_are_two_runs(self):
        obs = [{"t": 3.0}, {"t": 4.0}, {"t": 30.0}, {"t": 31.0}]
        self.assertEqual(len(K.group_confirms(obs)), 2)

    def feed_event(self, first, **kw):
        return dict({"id": "k001", "first": first, "last": first + 3, "t": first - 0.5, "dist_m": 40.0, "n_obs": 3, "weapon": "gun",
                     "headshot": False, "through_cover": False, "victim": "x", "icon": "killfeed_icons/k001.png"}, **kw)

    def test_a_feed_event_inside_a_popup_is_confirmed_and_keeps_its_detail(self):
        run = {"first": 2.0, "last": 5.0, "n_obs": 3, "amount": 2750, "xp": 250}
        out = K.merge_confirms([self.feed_event(2.5)], [run], 1.0)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["source"], out[0]["confirmed"], out[0]["dist_m"], out[0]["amount"]), ("both", True, 40.0, 2750))

    def test_a_popup_with_no_feed_row_is_a_kill_the_feed_did_not_show(self):
        out = K.merge_confirms([], [{"first": 50.0, "last": 53.0, "n_obs": 3, "amount": None, "xp": 250}], 1.0)
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual((e["source"], e["weapon"], e["dist_m"], e["icon"], e["id"]), ("confirm", "unknown", None, None, "c001"))
        self.assertAlmostEqual(e["t"], 49.5)

    def test_a_feed_row_with_no_popup_stays_a_feed_event(self):
        out = K.merge_confirms([self.feed_event(100.0)], [{"first": 10.0, "last": 12.0, "n_obs": 2, "amount": None, "xp": None}], 1.0)
        self.assertEqual(sorted(e["source"] for e in out), ["confirm", "feed"])
        self.assertFalse(next(e for e in out if e["source"] == "feed")["confirmed"])

    def test_the_merged_list_is_in_time_order(self):
        out = K.merge_confirms([self.feed_event(100.0)], [{"first": 10.0, "last": 12.0, "n_obs": 2, "amount": None, "xp": None}], 1.0)
        self.assertEqual([e["first"] for e in out], [10.0, 100.0])

    def test_one_popup_over_two_feed_rows_confirms_both_and_adds_nothing(self):
        run = {"first": 2.0, "last": 7.0, "n_obs": 4, "amount": None, "xp": None}
        out = K.merge_confirms([self.feed_event(2.5), self.feed_event(5.0)], [run], 1.0)
        self.assertEqual([e["source"] for e in out], ["both", "both"])

    def test_the_wardogs_merge_keeps_the_feed_clock(self):
        run = {"first": 2.0, "last": 5.0, "n_obs": 3, "amount": None, "xp": None}
        out = K.merge_confirms([self.feed_event(2.5)], [run], 1.0)
        self.assertAlmostEqual(out[0]["t"], 2.0)   # 2.5 - 0.5: from the feed row, as before the MW4 profile existed

    def test_mw4_takes_a_confirmed_kills_time_from_the_popup(self):
        run = {"first": 2.0, "last": 2.4, "n_obs": 3, "amount": None, "xp": None}
        out = K.merge_confirms([self.feed_event(3.5)], [run], 6.0, prefer_popup_time=True)
        self.assertEqual(out[0]["source"], "both")
        self.assertAlmostEqual(out[0]["t"], 2.0 - 0.5 / 6.0, places=2)


class TestMw4Feed(unittest.TestCase):
    """Call of Duty MW4's feed: no distance tag, the victim in red where OCR reads nothing - the name is all there is."""

    def setUp(self):
        self.match = K.make_matcher("Teck")

    def test_a_bare_name_in_the_killer_position_is_a_kill_for_mw4_only(self):
        row = [word("Teck", 87, w=80)]
        self.assertEqual(K.observe(row, 10.0, "f.jpg", self.match), [])
        out = K.observe(row, 10.0, "f.jpg", self.match, bare=True)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["dist"])
        self.assertEqual(out[0]["victim"], "")

    def test_a_death_row_is_still_a_death_when_bare(self):
        self.assertEqual(K.observe([word("q14", 87), word("Teck", 340)], 10.0, "f.jpg", self.match, bare=True), [])

    def test_somebody_elses_name_is_still_ignored_when_bare(self):
        self.assertEqual(K.observe([word("SeedheMaut", 87)], 10.0, "f.jpg", self.match, bare=True), [])

    def test_the_player_far_from_the_left_margin_is_not_the_killer_when_bare(self):
        self.assertEqual(K.observe([word("Teck", 700)], 10.0, "f.jpg", self.match, bare=True), [])


class TestMw4Popup(unittest.TestCase):
    """The kill line at the crosshair carries a skull; it is found by matching that icon in a narrow column (OCR cannot
    read the small font, and the word KILL is in every medal line, so it is not the marker)."""

    W, H = K.MW4_SKULL_REGION[2], K.MW4_SKULL_REGION[3]

    def region(self, skulls=(), seed=0, gain=1.0, offset=0.0):
        """A region of noise with the skull template pasted at each (x, y) in `skulls`."""
        import numpy as np
        t = K.skull_template()
        f = np.random.RandomState(seed).uniform(0, 60, size=(self.H, self.W))
        th, tw = t.shape
        for x, y in skulls:
            f[y:y + th, x:x + tw] = t * gain + offset
        return np.clip(f, 0, 255)

    def test_the_template_is_the_skull_sized_as_measured(self):
        t = K.skull_template()
        self.assertEqual(t.shape, (16, 15))
        self.assertGreater(t.max() - t.min(), 100)   # a real contrast, not a blank file

    def test_the_region_holds_a_skull_on_each_of_the_four_or_five_lines(self):
        th, tw = K.skull_template().shape
        self.assertGreaterEqual(self.W - tw, 5)       # a few pixels of slack either side of the fixed column
        self.assertGreaterEqual(self.H, 4 * 18)       # lines are ~18 px apart

    def test_region_fractions_are_the_pixel_box_in_a_1280x720_frame(self):
        x, y, w, h = K.skull_region_fractions()
        self.assertEqual((round(x * 1280), round(y * 720), round(w * 1280), round(h * 720)), K.MW4_SKULL_REGION)

    def test_the_template_scores_one_where_it_sits(self):
        self.assertGreater(K.ncc_best(self.region([(4, 10)]), K.skull_template()), 0.98)

    def test_it_is_found_on_any_line(self):
        for at in ((4, 0), (4, 22), (4, 40), (8, 60), (0, 28)):
            self.assertGreater(K.ncc_best(self.region([at]), K.skull_template()), 0.98, at)

    def test_the_fft_matcher_agrees_with_a_plain_loop(self):
        import numpy as np
        t = K.skull_template()
        th, tw = t.shape

        def plain(w):
            tz = t - t.mean()
            best = -1.0
            for y in range(w.shape[0] - th + 1):
                for x in range(w.shape[1] - tw + 1):
                    p = w[y:y + th, x:x + tw]
                    pz = p - p.mean()
                    d = np.sqrt((pz * pz).sum()) * np.sqrt((tz * tz).sum())
                    if d > 1e-3:
                        best = max(best, float((pz * tz).sum() / d))
            return best

        for seed, gain in ((3, 1.0), (4, 0.5), (5, 0.2)):
            w = self.region([(3 + seed, 7 + seed)], seed, gain=gain, offset=40)
            self.assertAlmostEqual(K.ncc_best(w, t), plain(w), places=6)
        noise = np.random.RandomState(11).uniform(0, 255, size=(self.H, self.W))
        self.assertAlmostEqual(K.ncc_best(noise, t), plain(noise), places=6)

    def test_brightness_and_contrast_do_not_matter(self):
        # the popup fades in and out over any background
        self.assertGreater(K.ncc_best(self.region([(4, 10)], gain=0.4, offset=30), K.skull_template()), 0.95)

    def test_noise_and_a_flat_window_are_not_a_skull(self):
        import numpy as np
        t = K.skull_template()
        scores = [K.ncc_best(np.random.RandomState(s).uniform(0, 255, size=(self.H, self.W)), t) for s in range(40)]
        self.assertLess(max(scores), K.MW4_POPUP_THRESHOLD)
        self.assertEqual(K.ncc_best(np.zeros((self.H, self.W)), t), -1.0)

    def test_a_window_smaller_than_the_template_scores_nothing(self):
        import numpy as np
        self.assertEqual(K.ncc_best(np.zeros((5, 5)), K.skull_template()), -1.0)
        self.assertIsNone(K.ncc_map(np.zeros((5, 5)), K.skull_template()))

    def test_every_skull_in_a_frame_is_found_once(self):
        # three kills on the stack: skulls one line (18 px) apart; a skull is not counted twice
        hits = K.skull_hits(self.region([(4, 4), (4, 22), (4, 40)]), K.skull_template())
        self.assertEqual(sorted(y for _s, y in hits), [4, 22, 40])
        self.assertTrue(all(s > 0.98 for s, _y in hits))

    def test_no_skull_means_no_hits(self):
        self.assertEqual(K.skull_hits(self.region(), K.skull_template()), [])

    def test_hits_come_out_one_list_per_raw_frame(self):
        import numpy as np
        raw = np.stack([self.region([(4, 10)], 1), self.region([(4, 10), (4, 28)], 2)]).astype(np.uint8).tobytes()
        raw += np.random.RandomState(9).uniform(0, 60, size=(self.H, self.W)).astype(np.uint8).tobytes()
        frames = K.popup_frames(raw)
        self.assertEqual([len(h) for h in frames], [1, 2, 0])

    def test_a_trailing_partial_frame_is_ignored(self):
        self.assertEqual(K.popup_frames(b"\x00" * (self.W * self.H) + b"\x01\x02"), [[]])

    # --- from skulls per frame to one run per kill ---------------------------------------------------------------

    def frames(self, *spec):
        """[(time, skull count)] -> the frames popup_runs takes: each skull a (score, y) one line apart."""
        return [(t, [(0.9, 4 + 18 * i) for i in range(n)]) for t, n in spec]

    def test_a_kill_line_seen_on_several_frames_is_one_run(self):
        runs = K.popup_runs(self.frames((10.0, 1), (10.17, 1), (10.33, 1), (10.5, 1), (10.67, 1)))
        self.assertEqual([(r["first"], r["last"], r["n_obs"]) for r in runs], [(10.0, 10.67, 5)])
        self.assertEqual(runs[0]["score"], 0.9)
        self.assertIsNone(runs[0]["amount"])

    def test_a_lone_frame_or_two_is_noise(self):
        self.assertEqual(K.popup_runs(self.frames((5.0, 1))), [])
        self.assertEqual(K.popup_runs(self.frames((5.0, 1), (5.17, 1))), [])
        self.assertEqual(len(K.popup_runs(self.frames((5.0, 1), (5.17, 1)), min_obs=2)), 1)

    def test_kills_seconds_apart_are_separate_popups(self):
        runs = K.popup_runs(self.frames((1.0, 1), (1.17, 1), (1.33, 1), (6.0, 1), (6.17, 1), (6.33, 1)))
        self.assertEqual([r["first"] for r in runs], [1.0, 6.0])

    def test_a_second_skull_that_lands_on_the_stack_is_a_second_kill(self):
        # one line, then a second a second later while the first is still up
        spec = [(10.0, 1), (10.17, 1), (10.33, 1), (10.5, 1), (10.67, 1), (10.83, 1), (11.0, 1), (11.17, 2), (11.33, 2),
                (11.5, 2), (11.67, 2), (11.83, 1), (12.0, 1)]
        runs = K.popup_runs(self.frames(*spec))
        self.assertEqual([r["first"] for r in runs], [10.0, 11.17])

    def test_a_skull_count_that_flickers_up_for_one_frame_is_not_a_kill(self):
        spec = [(10.0, 1), (10.17, 1), (10.33, 2), (10.5, 1), (10.67, 1), (10.83, 1)]
        self.assertEqual(len(K.popup_runs(self.frames(*spec))), 1)

    def test_three_kills_in_a_burst_are_three(self):
        spec = [(5.0, 1), (5.17, 1), (5.33, 1), (5.5, 2), (5.67, 2), (5.83, 2), (6.0, 3), (6.17, 3), (6.33, 3), (6.5, 3)]
        self.assertEqual([r["first"] for r in K.popup_runs(self.frames(*spec))], [5.0, 5.5, 6.0])

    def test_frames_with_no_skull_are_ignored(self):
        runs = K.popup_runs([(1.0, []), (2.0, []), (3.0, [(0.9, 4)]), (3.17, [(0.9, 4)]), (3.33, [(0.9, 4)])])
        self.assertEqual([r["first"] for r in runs], [3.0])

    def test_a_run_of_strong_skulls_is_sure(self):
        runs = K.popup_runs(self.frames((10.0, 1), (10.17, 1), (10.33, 1)))
        self.assertTrue(runs[0]["sure"])

    def test_a_run_that_only_just_matches_is_not_sure(self):
        # blank scenery and objective rings score ~0.75 on a frame or two: believed only if a feed row agrees
        weak = [(10.0 + i / 6.0, [(0.75, 4)]) for i in range(5)]
        runs = K.popup_runs(weak)
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0]["sure"])

    def test_one_strong_frame_in_a_weak_run_is_not_enough(self):
        mixed = [(10.0, [(0.83, 4)])] + [(10.0 + i / 6.0, [(0.74, 4)]) for i in range(1, 5)]
        self.assertFalse(K.popup_runs(mixed)[0]["sure"])

    def test_the_second_kills_sureness_is_its_own_skull_not_the_firsts(self):
        # the first line is strong the whole time, the second never gets above 0.75
        spec = [(10.0 + i / 6.0, [(0.95, 4)]) for i in range(6)] + [(11.0 + i / 6.0, [(0.95, 4), (0.74, 22)]) for i in range(4)]
        runs = K.popup_runs(spec)
        self.assertEqual([r["sure"] for r in runs], [True, False])

    def test_a_banner_with_no_skull_is_not_a_kill(self):
        # `DOUBLE KILL` arrives ~3 s after the kills it counts, alone: no skull, so no frames, so no run
        self.assertEqual(K.popup_runs([(t, []) for t in (20.0, 20.17, 20.33, 20.5)]), [])


class TestMw4Icons(unittest.TestCase):
    """The feed's weapon icon is a long thin silhouette; a headshot adds a skull after it. Blobs are (x0, x1, y0, y1, pixels)."""

    def test_a_gun_is_a_long_thin_blob(self):
        self.assertEqual(K.classify_icons_mw4([(10, 100, 5, 30, 900)]), ("gun", False, False))

    def test_a_skull_after_the_gun_is_a_headshot(self):
        self.assertEqual(K.classify_icons_mw4([(10, 100, 5, 30, 900), (140, 178, 0, 38, 700)]), ("gun", True, False))

    def test_a_squarer_icon_is_other_and_never_a_grenade(self):
        # a knife or a thrown weapon: not measured, so never given the grenade's slow-motion
        self.assertEqual(K.classify_icons_mw4([(10, 45, 2, 36, 500)])[0], "other")

    def test_specks_and_nothing_are_unknown(self):
        self.assertEqual(K.classify_icons_mw4([]), ("unknown", False, False))
        self.assertEqual(K.classify_icons_mw4([(10, 14, 5, 9, 12)]), ("unknown", False, False))

    def test_a_skull_shaped_blob_is_not_a_headshot_when_it_is_the_only_icon(self):
        # the first blob is the weapon: a round one first is not a skull after a gun
        self.assertEqual(K.classify_icons_mw4([(10, 48, 0, 38, 700)])[1], False)

    def test_a_wide_blob_after_the_gun_is_not_a_skull(self):
        self.assertFalse(K.classify_icons_mw4([(10, 100, 5, 30, 900), (120, 220, 5, 30, 800)])[1])

    def test_icon_blobs_has_a_softer_threshold_for_mw4(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (80, 40), (0, 0, 0))
        ImageDraw.Draw(im).rectangle([10, 10, 60, 25], fill=(170, 170, 170))   # a grey icon: not white enough for Wardogs
        self.assertEqual(K.icon_blobs(im), [])
        self.assertEqual(len(K.icon_blobs(im, K.MW4_ICON_THRESH)), 1)


class TestMw4Text(unittest.TestCase):
    """What the popup stack says: each line matched against labelled strips of itself (OCR cannot read the font)."""

    FIXTURES = Path(__file__).resolve().parent / "data" / "mw4_strips"

    def test_the_seed_file_is_phrases_the_reader_knows_and_junk(self):
        x, labels = K.text_seeds()
        self.assertEqual(x.shape, (len(labels), K.TEXT_VEC[0] * K.TEXT_VEC[1]))
        self.assertLessEqual(set(labels), set(K.MW4_PHRASES) | {""})
        self.assertGreaterEqual(len(set(labels) - {""}), 20)
        self.assertGreaterEqual(labels.count(""), 50)   # the junk seeds are what stop a player's name being read as a medal
        self.assertTrue(all(abs(float((v * v).sum()) - 1.0) < 0.02 for v in x[:50]))   # unit vectors, to the int8 rounding

    def test_every_phrase_has_a_category_attribute_lines_places(self):
        self.assertLessEqual(set(K.MW4_PHRASES.values()),
                             {"kind", "multi", "skill", "situation", "objective", "support"})
        self.assertEqual({p for p, c in K.MW4_PHRASES.items() if c == "kind"}, {"KILL", "HEADSHOT KILL", "MELEE KILL"})

    def read_fixture(self, name):
        import numpy as np
        from PIL import Image
        with Image.open(self.FIXTURES / name) as im:
            return K.read_strip(np.asarray(im.convert("L"), dtype=np.float64))

    def test_real_strips_read_as_what_they_say(self):
        for name, want in (("kill_0.png", "KILL"), ("headshot_kill_0.png", "HEADSHOT KILL"), ("melee_kill_0.png", "MELEE KILL"),
                           ("one_shot_one_kill_0.png", "ONE SHOT, ONE KILL"), ("quick_scope_0.png", "QUICK SCOPE"),
                           ("survivor_0.png", "SURVIVOR"), ("revenge_0.png", "REVENGE"), ("kingslayer_0.png", "KINGSLAYER"),
                           ("savior_0.png", "SAVIOR")):
            text, cos = self.read_fixture(name)
            self.assertEqual(text, want, name)
            self.assertGreaterEqual(cos, K.TEXT_MIN_COS, name)

    def test_a_players_name_or_scenery_reads_as_nothing(self):
        for name in ("junk_0.png", "junk_1.png"):
            self.assertEqual(self.read_fixture(name)[0], "", name)

    def test_blank_and_noise_strips_read_as_nothing(self):
        import numpy as np
        self.assertEqual(K.read_strip(np.full((16, 132), 60.0))[0], "")
        self.assertEqual(K.read_strip(np.random.RandomState(1).uniform(0, 255, size=(16, 132)))[0], "")

    def text_strip(self, left=0, width=24, extra=None):
        """A synthetic strip: bright vertical strokes 2 px wide every 4 px from `left`, rows 3-12, on a dark ground."""
        import numpy as np
        s = np.full((16, 132), 40.0)
        for x in range(left, left + width, 4):
            s[3:13, x:x + 2] = 220.0
        for x in (extra or ()):
            s[3:13, x:x + 2] = 220.0
        return s

    def test_a_short_word_is_enough_ink_to_be_a_line(self):
        # `KILL` is ~22 px wide: the first version needed 18+ ink columns and so missed half of the plain kills
        self.assertIsNotNone(K.strip_vector(self.text_strip(width=22)))

    def test_ink_that_is_not_at_the_left_edge_is_not_a_line(self):
        self.assertIsNone(K.strip_vector(self.text_strip(left=40, width=40)))

    def test_too_little_ink_is_not_a_line(self):
        self.assertIsNone(K.strip_vector(self.text_strip(width=6)))

    def test_hud_further_right_is_not_part_of_the_text(self):
        # an objective marker 40 px after the word must not change what the word reads as
        import numpy as np
        a = K.strip_vector(self.text_strip(width=22))
        b = K.strip_vector(self.text_strip(width=22, extra=range(64, 100, 4)))
        self.assertIsNotNone(b)
        self.assertTrue(np.allclose(a, b, atol=1e-6))

    def test_the_vector_is_a_unit_vector_of_the_expected_size(self):
        import numpy as np
        v = K.strip_vector(self.text_strip(width=40))
        self.assertEqual(v.shape, (K.TEXT_VEC[0] * K.TEXT_VEC[1],))
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=5)

    # --- lines across frames ---------------------------------------------------------------------------------------

    def block(self, slots):
        """A 90 x 160 block with a marker pixel in the text column of each slot named in `slots` ({y: code})."""
        import numpy as np
        g = np.zeros((90, 160))
        for y, code in slots.items():
            g[y + 3, K.TEXT_COLS[0] + 10] = code
        return g

    def stub_reader(self, strip, **kw):
        return {1.0: "KILL", 2.0: "ONE SHOT, ONE KILL", 3.0: "DOUBLE KILL"}.get(float(strip[3, 10]), ""), 0.9

    def read(self, blocks, **kw):
        from unittest import mock
        with mock.patch.object(K, "read_strip", self.stub_reader):
            return K.read_lines(blocks, 100.0, 6.0, phase=10, **kw)

    def test_a_line_is_one_instance_from_its_first_frame_to_its_last(self):
        out = self.read([self.block({10: 1}), self.block({10: 1}), self.block({10: 1}), self.block({})])
        self.assertEqual([(l["text"], l["first"], l["last"], l["n"], l["kind"]) for l in out], [("KILL", 100.0, 100.333, 3, "kind")])

    def test_a_line_that_scrolls_up_a_slot_is_still_one_instance(self):
        out = self.read([self.block({10: 1, 28: 2}), self.block({10: 1, 28: 2}), self.block({28: 1, 46: 2}), self.block({28: 1, 46: 2})])
        self.assertEqual(sorted((l["text"], l["n"]) for l in out), [("KILL", 4), ("ONE SHOT, ONE KILL", 4)])

    def test_a_phrase_seen_on_one_frame_is_a_misread(self):
        self.assertEqual(self.read([self.block({10: 1}), self.block({}), self.block({})]), [])
        self.assertEqual(len(self.read([self.block({10: 1}), self.block({})], min_frames=1)), 1)

    def test_the_same_phrase_twice_with_a_gap_is_two_instances(self):
        gap = [self.block({})] * 8
        out = self.read([self.block({10: 3}), self.block({10: 3})] + gap + [self.block({10: 3}), self.block({10: 3})])
        self.assertEqual([l["text"] for l in out], ["DOUBLE KILL", "DOUBLE KILL"])
        self.assertEqual(out[0]["kind"], "multi")

    def test_no_grid_no_lines(self):
        import numpy as np
        self.assertEqual(K.read_lines([np.zeros((90, 160))] * 3, 0.0, 6.0), [])   # no skull anywhere: no phase to read at

    def test_overlapping_windows_read_one_line_once(self):
        lines = [{"text": "KILL", "first": 10.0, "last": 10.5, "n": 4, "kind": "kind"},
                 {"text": "KILL", "first": 10.3, "last": 11.0, "n": 5, "kind": "kind"},
                 {"text": "KILL", "first": 20.0, "last": 20.5, "n": 4, "kind": "kind"},
                 {"text": "REVENGE", "first": 10.4, "last": 11.0, "n": 3, "kind": "situation"}]
        out = K.merge_instances(lines)
        self.assertEqual([(l["text"], l["first"], l["last"], l["n"]) for l in out],
                         [("KILL", 10.0, 11.0, 5), ("REVENGE", 10.4, 11.0, 3), ("KILL", 20.0, 20.5, 4)])

    # --- which kill does a line belong to -------------------------------------------------------------------------

    def ev(self, t):
        return {"id": "e{0:g}".format(t), "t": t}

    def line(self, text, first):
        return {"text": text, "first": first, "last": first + 1.0, "n": 6, "kind": K.MW4_PHRASES[text]}

    def test_a_kind_line_goes_to_the_nearest_kill(self):
        a, b = self.ev(10.0), self.ev(12.0)
        K.attribute_lines([a, b], [self.line("HEADSHOT KILL", 10.2), self.line("KILL", 12.1)])
        self.assertEqual((a["popup"]["kind"], b["popup"]["kind"]), ("HEADSHOT KILL", "KILL"))

    def test_medals_go_to_the_latest_kill_that_had_begun(self):
        a, b = self.ev(10.0), self.ev(14.0)
        K.attribute_lines([a, b], [self.line("KILL", 10.1), self.line("ONE SHOT, ONE KILL", 10.6), self.line("KILL", 14.1),
                                   self.line("QUICK SCOPE", 14.5), self.line("SURVIVOR", 14.6)])
        self.assertEqual(a["popup"]["medals"], ["ONE SHOT, ONE KILL"])
        self.assertEqual(b["popup"]["medals"], ["QUICK SCOPE"])
        self.assertEqual(b["popup"]["situations"], ["SURVIVOR"])

    def test_the_multi_kill_banner_lands_on_the_last_of_its_kills(self):
        a, b = self.ev(10.0), self.ev(11.5)
        K.attribute_lines([a, b], [self.line("KILL", 10.1), self.line("KILL", 11.6), self.line("DOUBLE KILL", 14.4)])
        self.assertIsNone(a["popup"]["multi"])
        self.assertEqual(b["popup"]["multi"], "DOUBLE KILL")

    def test_a_line_long_after_every_kill_is_nobodys(self):
        a = self.ev(10.0)
        K.attribute_lines([a], [self.line("KILL", 10.1), self.line("REVENGE", 40.0)])
        self.assertEqual(a["popup"]["situations"], [])

    def test_a_line_just_before_a_kill_is_the_previous_kills_not_this_ones(self):
        a, b = self.ev(15.0), self.ev(20.0)
        K.attribute_lines([a, b], [self.line("AVENGER", 19.0)])
        self.assertEqual((a["popup"]["situations"], b["popup"]["situations"]), (["AVENGER"], []))

    def test_objective_support_and_repeats(self):
        a = self.ev(10.0)
        K.attribute_lines([a], [self.line("KILL", 10.1), self.line("OFFENSE", 10.5), self.line("OFFENSE", 11.0),
                                self.line("UAV REVEAL ASSIST", 11.2)])
        self.assertEqual(a["popup"]["objective"], ["OFFENSE"])      # a repeated line is listed once
        self.assertEqual(a["popup"]["support"], ["UAV REVEAL ASSIST"])
        self.assertEqual(len(a["popup"]["lines"]), 4)               # ... but both sightings are kept

    def test_a_kill_with_no_text_has_an_empty_popup(self):
        a = self.ev(10.0)
        K.attribute_lines([a], [])
        self.assertEqual(a["popup"], {"kind": None, "medals": [], "situations": [], "objective": [], "multi": None, "support": [],
                                      "lines": []})

    def test_a_kind_line_far_from_any_kill_is_dropped(self):
        a = self.ev(10.0)
        K.attribute_lines([a], [self.line("KILL", 30.0)])
        self.assertIsNone(a["popup"]["kind"])


class TestMw4TextStage(unittest.TestCase):
    def test_the_text_pass_decodes_each_kills_window_and_marks_headshots(self):
        from unittest import mock
        import numpy as np
        from clipbot.config import load_settings
        from clipbot.stages import killfeed as stage
        bw, bh = K.MW4_TEXT_BLOCK[2], K.MW4_TEXT_BLOCK[3]
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout=np.zeros((3, bh, bw), dtype=np.uint8).tobytes(), stderr=b"")

        def fake_lines(blocks, t0, fps, **kw):
            return [{"text": "HEADSHOT KILL", "first": t0 + 0.2, "last": t0 + 1.0, "n": 5, "kind": "kind"},
                    {"text": "QUICK SCOPE", "first": t0 + 0.6, "last": t0 + 1.2, "n": 4, "kind": "skill"}]

        events = [{"id": "k001", "t": 50.0, "headshot": False}, {"id": "c001", "t": 90.0, "headshot": False}]
        with mock.patch.object(stage.subprocess, "run", fake_run), mock.patch.object(K, "read_lines", fake_lines):
            stats = stage._read_popup_text(events, Path("v.mp4"), load_settings())
        self.assertEqual(len(calls), 2)
        self.assertIn("v.mp4", calls[0])
        self.assertEqual(calls[0][calls[0].index("-ss") + 1], "49.900")
        self.assertIn("scale={0}:{1}".format(bw, bh), calls[0][calls[0].index("-vf") + 1])
        self.assertEqual([e["popup"]["kind"] for e in events], ["HEADSHOT KILL", "HEADSHOT KILL"])
        self.assertEqual([e["headshot"] for e in events], [True, True])
        self.assertEqual(events[0]["popup"]["medals"], ["QUICK SCOPE"])
        self.assertEqual(stats["lines"], 4)
        self.assertEqual(stats["kills_with_kind"], 2)
        self.assertEqual(stats["phrases"], {"HEADSHOT KILL": 2, "QUICK SCOPE": 2})

    def test_a_failed_decode_is_an_error_not_an_empty_popup(self):
        from unittest import mock
        from clipbot.config import load_settings
        from clipbot.stages import killfeed as stage
        from clipbot.utils import StageError
        bad = mock.Mock(returncode=1, stdout=b"", stderr=b"boom")
        with mock.patch.object(stage.subprocess, "run", return_value=bad):
            with self.assertRaises(StageError):
                stage._read_popup_text([{"id": "k001", "t": 5.0, "headshot": False}], Path("v.mp4"), load_settings())

    def test_the_text_pass_refuses_a_wardogs_killfeed(self):
        import json
        import tempfile
        from clipbot.config import load_settings
        from clipbot.stages import killfeed as stage
        from clipbot.utils import StageError
        from clipbot.workspace import Workspace
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d) / "w").ensure()
            (ws.root / "killfeed.json").write_text(json.dumps({"game": "wardogs", "events": []}), encoding="utf-8")
            with self.assertRaises(StageError) as cm:
                stage.read_popup_text(ws, load_settings(), video=Path("x.mp4"))
        self.assertIn("mw4", str(cm.exception))


class TestMw4Stage(unittest.TestCase):
    def test_the_icon_crop_starts_after_the_name_and_reaches_past_a_skull(self):
        from PIL import Image
        from clipbot.stages import killfeed as stage
        im = Image.new("RGB", (1190, 560))
        o = {"kx": 165, "dx": 300, "cy": 280, "h": 30}
        c = stage._icon_crop(im, o, mw4=True)
        self.assertEqual(c.size, (250, 54))   # kx+25 .. kx+275 wide, +-0.9 of the name's height tall
        wardogs = stage._icon_crop(im, o)
        self.assertEqual(wardogs.size, (179, 45))   # the original geometry, untouched: 166..345 wide, +-0.75 tall

    def test_chunks_can_start_part_way_through(self):
        from clipbot.stages import killfeed as stage
        self.assertEqual(stage._chunks(100.0, 40.0, start=30.0), [(30.0, 40.0), (70.0, 30.0)])
        self.assertEqual(stage._chunks(100.0, 40.0, start=99.9), [])

    def test_an_unknown_game_is_refused_before_any_work(self):
        import tempfile
        from pathlib import Path
        from clipbot.config import load_settings
        from clipbot.stages import killfeed as stage
        from clipbot.utils import StageError
        from clipbot.workspace import Workspace
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d) / "w").ensure()
            with self.assertRaises(StageError) as cm:
                stage.scan_killfeed(ws, load_settings(), game="doom", force=True)
        self.assertIn("wardogs", str(cm.exception))
        self.assertIn("mw4", str(cm.exception))

    def test_the_games_are_the_two_profiles(self):
        from clipbot.stages import killfeed as stage
        self.assertEqual(stage.GAMES, ("wardogs", "mw4"))

    def test_the_shipped_settings_default_to_wardogs_and_carry_the_mw4_block(self):
        from clipbot.config import load_settings
        s = load_settings()
        self.assertEqual(s.get("killfeed.game"), "wardogs")
        self.assertEqual(s.get("killfeed.crop"), "620:230:0:440")   # the Wardogs feed crop is untouched
        self.assertEqual(float(s.get("killfeed.mw4.popup_threshold")), K.MW4_POPUP_THRESHOLD)
        self.assertEqual(float(s.get("killfeed.mw4.popup_gap")), K.MW4_POPUP_GAP)
        fx, fy, fw, fh = [float(v) for v in str(s.get("killfeed.mw4.feed_crop")).split(":")]
        self.assertEqual((fx, fy, fw, fh), K.MW4_FEED_CROP)


if __name__ == "__main__":
    unittest.main()
