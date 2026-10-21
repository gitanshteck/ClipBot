"""Tests for clipbot/killfeed.py (pure parsing) and clipbot/stages/killfeed.py helpers.

OCR itself is not exercised - it is a Windows API call - but everything after it is
plain data, so the rules learned from reading real feeds are pinned here with
synthetic words: the distance tag as OCR mangles it, a kill versus a death, the
bracket misread as a leading 1, grouping a row seen over several frames, icons.
"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
