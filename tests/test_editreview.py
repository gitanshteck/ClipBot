"""editreview.py: parsing a time, finding the clip it falls in, and the life of a comment."""

import tempfile
import unittest
from pathlib import Path

from clipbot import editreview as R
from clipbot.specerror import SpecError


def timeline():
    def clip(cid, start, dur, tags=(), label=None, typ="clip"):
        return {"id": cid, "type": typ, "start": start, "dur": dur, "tags": list(tags), "label": label or cid.upper()}

    # the intro card is 12 s so the first chapter does not fold into it (edittimeline.chapters folds titles < 10 s apart)
    return {"clips": [clip("sting", 0.0, 12.0, typ="card", label="Intro"), clip("a", 12.0, 20.0, ["chapter"], "Headshots"),
                      clip("b", 32.0, 10.0), clip("c", 42.0, 25.0, ["chapter"], "Nades"), clip("d", 67.0, 7.0)]}


class TestTime(unittest.TestCase):
    def test_every_spelling_of_a_time(self):
        self.assertEqual(R.parse_time(192.4), 192.4)
        self.assertEqual(R.parse_time("192.4"), 192.4)
        self.assertEqual(R.parse_time("3:12"), 192.0)
        self.assertEqual(R.parse_time("3:12.4"), 192.4)
        self.assertEqual(R.parse_time("1:03:12"), 3792.0)
        self.assertEqual(R.parse_time(" 0:05 "), 5.0)

    def test_nonsense_and_negative_times_are_refused(self):
        for bad in ("soon", "3:", "-4", "1:2:3:4", "", "3:99x"):
            with self.assertRaises(SpecError, msg=repr(bad)):
                R.parse_time(bad)
        with self.assertRaises(SpecError):
            R.parse_time(-1.0)

    def test_mmss(self):
        self.assertEqual(R.mmss(192.4), "3:12.4")
        self.assertEqual(R.mmss(5.0), "0:05.0")
        self.assertEqual(R.mmss(3792.0), "1:03:12")


class TestLocate(unittest.TestCase):
    def test_a_second_belongs_to_the_clip_on_screen_with_its_offset_and_chapter(self):
        w = R.locate(timeline(), 37.5)
        self.assertEqual((w["clip"], w["offset"], w["chapter"], w["label"]), ("b", 5.5, "Headshots", "B"))

    def test_clip_boundaries_belong_to_the_later_clip(self):
        self.assertEqual(R.locate(timeline(), 32.0)["clip"], "b")
        self.assertEqual(R.locate(timeline(), 31.99)["clip"], "a")

    def test_the_start_has_the_intro_chapter_and_the_end_is_the_last_clip(self):
        self.assertEqual(R.locate(timeline(), 0.0)["chapter"], "Intro")
        w = R.locate(timeline(), 500.0)
        self.assertEqual((w["clip"], w["chapter"]), ("d", "Nades"))

    def test_without_a_timeline_nothing_is_located(self):
        self.assertEqual(R.locate(None, 5.0), {"clip": None, "offset": None, "chapter": None, "label": None})
        self.assertEqual(R.locate({"clips": []}, 5.0)["clip"], None)


class TestComments(unittest.TestCase):
    def doc(self):
        return {"schema": 1, "name": "e", "comments": []}

    def test_adding_records_where_the_comment_was_about_and_numbers_them(self):
        d = self.doc()
        c = R.add(d, "0:37.5", "zoom less", timeline=timeline(), now=0)
        self.assertEqual((c["id"], c["t"], c["clip"], c["offset"], c["chapter"], c["kind"], c["status"], c["on"]),
                         ("n1", 37.5, "b", 5.5, "Headshots", "change", "open", "preview"))
        self.assertEqual(R.add(d, 40, "too slow", kind="taste", on="v4")["id"], "n2")
        R.delete(d, "n1")
        self.assertEqual(R.add(d, 41, "x")["id"], "n3")                          # ids are never reused

    def test_bad_comments_are_refused_with_what_to_fix(self):
        d = self.doc()
        for kwargs in ({"text": ""}, {"text": "  "}, {"text": "x", "kind": "rant"}, {"text": "x", "on": "tomorrow"},
                       {"text": "x" * (R.MAX_TEXT + 1)}):
            with self.assertRaises(SpecError, msg=str(kwargs)[:30]):
                R.add(d, 5, **kwargs)
        self.assertEqual(d["comments"], [])

    def test_resolving_records_how_and_in_which_version_and_reopening_clears_it(self):
        d = self.doc()
        R.add(d, 5, "zoom less")
        c = R.resolve(d, "n1", how="camera zoom 1.18 -> 1.10", in_version="v5", now=0)
        self.assertEqual((c["status"], c["done"]["in"], c["done"]["how"]), ("done", "v5", "camera zoom 1.18 -> 1.10"))
        R.reopen(d, "n1")
        self.assertEqual((d["comments"][0]["status"], d["comments"][0]["done"]), ("open", None))
        R.resolve(d, "n1", how="he likes it", status="wontfix")
        self.assertEqual(d["comments"][0]["status"], "wontfix")
        with self.assertRaises(SpecError):
            R.resolve(d, "n1", status="maybe")

    def test_unknown_comments_and_edits(self):
        d = self.doc()
        for f in (lambda: R.resolve(d, "n9"), lambda: R.reopen(d, "n9"), lambda: R.delete(d, "n9"), lambda: R.reply(d, "n9", "x"),
                  lambda: R.update(d, "n9", text="x")):
            with self.assertRaises(SpecError):
                f()
        R.add(d, 5, "a")
        R.update(d, "n1", text="b", kind="question")
        R.reply(d, "n1", "because the gap rule")
        self.assertEqual((d["comments"][0]["text"], d["comments"][0]["kind"], d["comments"][0]["reply"]), ("b", "question", "because the gap rule"))
        with self.assertRaises(SpecError):
            R.update(d, "n1", text=" ")

    def test_open_comments_are_the_open_ones_in_viewing_order(self):
        d = self.doc()
        R.add(d, 50, "late")
        R.add(d, 10, "early")
        R.add(d, 30, "done one")
        R.add(d, 5, "on an older version", on="v2")
        R.resolve(d, "n3", how="x")
        self.assertEqual([c["text"] for c in R.open_comments(d)], ["early", "late", "on an older version"])

    def test_a_line_names_the_clip_and_what_was_done(self):
        d = self.doc()
        R.add(d, 37.5, "zoom less", timeline=timeline())
        self.assertIn("[b +5.5s]", R.line(d["comments"][0]))
        R.resolve(d, "n1", how="zoom 1.18 -> 1.10", in_version="v5")
        self.assertIn("(done: zoom 1.18 -> 1.10)", R.line(d["comments"][0]))


class TestStore(unittest.TestCase):
    def test_round_trip_and_a_missing_file_is_an_empty_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(R.load(root, "e")["comments"], [])
            d = R.load(root, "e")
            R.add(d, "1:00", "ünïcode — ok")
            R.save(root, d)
            self.assertEqual(R.load(root, "e")["comments"][0]["text"], "ünïcode — ok")

    def test_a_corrupt_file_is_a_clear_error_not_a_silent_empty_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            R.review_path(Path(tmp)).write_text("{not json", encoding="utf-8")
            with self.assertRaises(SpecError):
                R.load(Path(tmp))
            R.review_path(Path(tmp)).write_text('{"comments": 3}', encoding="utf-8")
            with self.assertRaises(SpecError):
                R.load(Path(tmp))


if __name__ == "__main__":
    unittest.main()
