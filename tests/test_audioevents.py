"""audioevents.py: categories from class probabilities, peaks, and the queries the editor makes of events.json."""

import unittest

import numpy as np

from clipbot import audioevents as A
from clipbot import panns


class TestCategories(unittest.TestCase):
    def test_a_category_is_the_highest_of_its_classes(self):
        classes = A.union_classes()
        probs = np.zeros((3, len(classes)), dtype=np.float32)
        col = {c: i for i, c in enumerate(classes)}
        probs[0, col[18]] = 0.4                    # Giggle
        probs[1, col[16]] = 0.2                    # Laughter
        probs[1, col[21]] = 0.7                    # Chuckle
        probs[2, col[427]] = 0.9                   # Gunshot
        s = A.category_series(probs, classes)
        self.assertEqual([round(float(v), 2) for v in s["laughter"]], [0.4, 0.7, 0.0])
        self.assertEqual([round(float(v), 2) for v in s["gunfire"]], [0.0, 0.0, 0.9])
        self.assertEqual(set(s), set(panns.CATEGORIES))

    def test_no_seconds_is_no_series(self):
        classes = A.union_classes()
        s = A.category_series(np.zeros((0, len(classes)), dtype=np.float32), classes)
        self.assertTrue(all(len(v) == 0 for v in s.values()))

    def test_the_union_has_every_class_once_and_in_order(self):
        u = A.union_classes()
        self.assertEqual(u, sorted(set(u)))
        for idx in panns.CATEGORIES.values():
            self.assertTrue(set(idx) <= set(u))

    def test_quantize_clips_and_rounds(self):
        self.assertEqual(A.quantize(np.array([-0.2, 0.0, 0.004, 0.005, 0.996, 1.7])), [0, 0, 0, 0, 100, 100])


class TestPeaks(unittest.TestCase):
    def test_runs_above_the_threshold_are_peaks_with_their_highest_second(self):
        s = [0, 0.1, 0.6, 0.9, 0.5, 0, 0, 0, 0, 0, 0.4, 0.3, 0]
        pk = A.find_peaks(s, 0.3)
        self.assertEqual([(p["start"], p["end"], p["t"], p["p"]) for p in pk], [(2, 5, 3, 0.9), (10, 12, 10, 0.4)])

    def test_two_runs_a_short_gap_apart_are_one_peak(self):
        s = [0.8, 0, 0, 0.7, 0, 0, 0, 0, 0.9]
        self.assertEqual(len(A.find_peaks(s, 0.5, merge_gap=2)), 2)         # 0..3 joined (gap 2), 8 apart
        self.assertEqual(len(A.find_peaks(s, 0.5, merge_gap=1)), 3)

    def test_a_scan_that_starts_late_puts_peaks_on_the_streams_clock(self):
        pk = A.find_peaks([0, 0.9, 0], 0.5, offset=100)
        self.assertEqual((pk[0]["start"], pk[0]["t"]), (101, 101))

    def test_nothing_above_the_threshold_is_no_peak(self):
        self.assertEqual(A.find_peaks([0.1, 0.2], 0.5), [])
        self.assertEqual(A.find_peaks([], 0.5), [])


def doc(first=0):
    series = {name: np.zeros(40, dtype=np.float32) for name in panns.CATEGORIES}
    series["laughter"][10:13] = [0.3, 0.8, 0.5]
    series["laughter"][30] = 0.6
    series["shout"][20] = 0.9
    series["gunfire"][5] = 0.95
    return A.build_document("ws", 40.0, series, first=first)


class TestDocument(unittest.TestCase):
    def test_the_document_has_every_category_peaks_and_the_scan_start(self):
        d = doc()
        self.assertEqual(set(d["categories"]), set(panns.CATEGORIES))
        self.assertEqual(d["seconds"], 40)
        self.assertEqual(d["categories"]["laughter"][11], 80)
        self.assertEqual([p["t"] for p in d["peaks"]["laughter"]], [11, 30])

    def test_score_at_reads_the_stored_integer_as_a_probability(self):
        d = doc()
        self.assertEqual(A.score_at(d, "laughter", 11.4), 0.8)
        self.assertEqual(A.score_at(d, "laughter", 99), 0.0)
        self.assertEqual(A.score_at(d, "nonsense", 3), 0.0)

    def test_a_partial_scan_is_addressed_by_stream_seconds(self):
        d = doc(first=3600)
        self.assertEqual(A.score_at(d, "laughter", 3611), 0.8)
        self.assertEqual(A.score_at(d, "laughter", 11), 0.0)                 # before what was scanned
        self.assertEqual(d["peaks"]["laughter"][0]["t"], 3611)

    def test_peak_between_finds_the_highest_second_in_a_range(self):
        d = doc()
        self.assertEqual(A.peak_between(d, "laughter", 0, 40), (0.8, 11.0))
        self.assertEqual(A.peak_between(d, "laughter", 12, 20), (0.5, 12.0))
        self.assertEqual(A.peak_between(d, "laughter", 14, 25), (0.0, 14.0))
        self.assertEqual(A.peak_between(d, "laughter", 50, 60), (0.0, None))

    def test_a_reaction_is_the_stronger_of_laughter_and_shouting(self):
        d = doc()
        self.assertEqual(A.reaction_between(d, 0, 40)["category"], "shout")
        self.assertEqual(A.reaction_between(d, 0, 15)["category"], "laughter")
        self.assertEqual(A.reaction_between(d, 0, 8), {"p": 0.0})            # gunfire is not a reaction

    def test_top_peaks_are_strongest_first_and_not_crowded(self):
        d = doc()
        top = A.top_peaks(d, "laughter", n=5, min_gap=30)
        self.assertEqual([p["t"] for p in top], [11])                        # 30 is within 30 s of 11 (19 s apart)
        top = A.top_peaks(d, "laughter", n=5, min_gap=10)
        self.assertEqual([p["t"] for p in top], [11, 30])
        self.assertEqual(A.top_peaks(d, "laughter", n=5, min_p=0.7, min_gap=1)[0]["p"], 0.8)
        self.assertEqual(len(A.top_peaks(d, "laughter", n=5, min_p=0.99)), 0)


class TestChecks(unittest.TestCase):
    def test_auc_is_one_for_a_perfect_score_half_for_chance_and_handles_ties_and_empties(self):
        self.assertEqual(A.auc([3, 4], [1, 2]), 1.0)
        self.assertEqual(A.auc([1, 2], [3, 4]), 0.0)
        self.assertEqual(A.auc([1, 1], [1, 1]), 0.5)
        self.assertAlmostEqual(A.auc([1, 3], [2, 4]), 0.25)
        self.assertTrue(np.isnan(A.auc([], [1])))
        rng = np.random.RandomState(0)
        self.assertAlmostEqual(A.auc(rng.rand(2000), rng.rand(2000)), 0.5, delta=0.03)

    def test_window_max_is_inclusive_and_zero_outside_the_series(self):
        s = [0, 5, 0, 0, 9]
        self.assertEqual(A.window_max(s, 0, 1), 5)
        self.assertEqual(A.window_max(s, 2, 4), 9)
        self.assertEqual(A.window_max(s, -10, -1), 0.0)
        self.assertEqual(A.window_max(s, 50, 60), 0.0)

    def test_chat_laughs_are_found_and_put_on_the_vod_clock(self):
        doc = {"status": "ok", "messages": [{"offset": 100.0, "text": "KEKW"}, {"offset": 130.0, "text": "nice shot"},
                                           {"offset": 160.0, "text": "that was lmao funny"}, {"offset": 170.0, "text": "😂😂"}]}
        self.assertEqual(A.chat_laugh_times(doc, offset=10.0), [90.0, 150.0, 160.0])
        self.assertEqual(A.chat_laugh_times({"status": "unavailable", "messages": doc["messages"]}), [])
        self.assertEqual(A.chat_laugh_times({"status": "ok", "messages": [{"offset": 1.0, "text": "dead game"}]}), [])

    def test_a_score_that_peaks_at_the_moments_separates_them_from_random_time(self):
        n = 2000
        moments = list(range(100, 1900, 100))
        good = np.zeros(n)
        for t in moments:
            good[t - 5] = 0.9
        flat = np.full(n, 0.1)
        res = A.check_series({"good": good, "flat": flat}, moments, before=12, after=0)
        self.assertGreater(res["good"]["auc"], 0.9)
        self.assertEqual(res["good"]["at_moments"], 1.0)
        self.assertLess(res["good"]["at_random"], 0.4)
        self.assertEqual(res["flat"]["auc"], 0.5)

    def test_top_hit_rate_counts_peaks_followed_by_a_moment_and_random_is_the_base_rate(self):
        self.assertEqual(A.top_hit_rate([10, 100, 500], [15, 505]), (2, 3))
        self.assertEqual(A.top_hit_rate([10, 100], []), (0, 2))
        self.assertEqual(A.top_hit_rate([10], [5]), (0, 1))                    # a moment *before* the peak is not a reaction to it
        base = A.random_hit_rate(10000, 20, list(range(50, 10000, 100)), after=12.0)
        self.assertAlmostEqual(base, 0.13, delta=0.04)                         # 13 s of every 100 are followed by a moment
        self.assertEqual(A.random_hit_rate(10, 5, [1]), 0.0)


if __name__ == "__main__":
    unittest.main()
