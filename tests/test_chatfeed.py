"""clipbot/chatfeed.py: the pure live-chat logic (pulse, votes, questions, repeats, newcomers).

Time is injected, so nothing here sleeps or touches the network.
"""

import unittest

from clipbot import chatfeed
from clipbot.chatfeed import ChatFeed

NOW = 100000.0


class Clock(object):
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def msg(mid, text, slug="viewer", age=0.0, now=NOW, user=None, badges=None):
    return {"id": str(mid), "user": user or slug, "slug": slug, "text": text, "at": now - age, "badges": badges or []}


class TestAdd(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.feed = ChatFeed(bots=["BotRix"], clock=self.clock)

    def test_dedupes_by_id(self):
        self.assertTrue(self.feed.add(msg(1, "hello")))
        self.assertFalse(self.feed.add(msg(1, "hello again", slug="other")))
        self.assertEqual(len(self.feed.messages), 1)
        self.assertEqual(self.feed.total, 1)

    def test_rejects_empty_text_and_missing_id(self):
        self.assertFalse(self.feed.add(msg(1, "")))
        self.assertFalse(self.feed.add(msg(2, "   ")))
        self.assertFalse(self.feed.add(msg("", "text")))
        self.assertEqual(len(self.feed.messages), 0)

    def test_rejects_bots_case_insensitively(self):
        self.assertFalse(self.feed.add(msg(1, "!uptime", slug="botrix")))
        self.assertFalse(self.feed.add(msg(2, "!uptime", slug="BOTRIX")))
        self.assertEqual(len(self.feed.messages), 0)
        self.assertEqual(self.feed.snapshot()["chatters"], 0)

    def test_keeps_only_the_last_keep_messages(self):
        for i in range(chatfeed.KEEP + 25):
            self.feed.add(msg("m{0}".format(i), "hello {0}".format(i), slug="u{0}".format(i)))
        self.assertEqual(len(self.feed.messages), chatfeed.KEEP)
        self.assertEqual(self.feed.messages[-1]["id"], "m{0}".format(chatfeed.KEEP + 24))
        self.assertEqual(self.feed.messages[0]["id"], "m25")

    def test_first_time_chatters_are_newcomers(self):
        self.feed.add(msg(1, "hi", slug="ann", user="Ann"))
        self.feed.add(msg(2, "hi again", slug="ann", user="Ann"))
        self.feed.add(msg(3, "yo", slug="ben", user="Ben"))
        self.assertEqual(self.feed.newcomer_list(), ["Ben", "Ann"])
        self.assertEqual(len(self.feed.newcomers), 2)

    def test_count_newcomer_false_suppresses_but_still_remembers(self):
        self.feed.add(msg(1, "hi", slug="ann", user="Ann"), count_newcomer=False)
        self.assertEqual(self.feed.newcomer_list(), [])
        self.feed.add(msg(2, "hi again", slug="ann", user="Ann"))
        self.assertEqual(self.feed.newcomer_list(), [])   # already known, so still not new
        self.assertEqual(len(self.feed.messages), 2)

    def test_text_is_trimmed_and_capped(self):
        self.feed.add(msg(1, "  " + "x" * 500 + "  "))
        self.assertEqual(len(self.feed.messages[0]["text"]), 300)


class TestPulse(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.feed = ChatFeed(clock=self.clock)

    def add_aged(self, ages):
        for i, age in enumerate(ages):
            self.feed.add(msg("a{0}".format(i), "hello", slug="u{0}".format(i), age=age))

    def test_rate_per_minute(self):
        self.add_aged([1, 10, 20, 59, 61, 300])
        self.assertEqual(self.feed.rate_per_minute(), 4.0)

    def test_sparkline_places_messages_in_the_right_bin_oldest_left(self):
        self.add_aged([5, 6, 45, 575, 700])   # the last one is older than ten minutes and is dropped
        bins = self.feed.sparkline()
        self.assertEqual(len(bins), chatfeed.SPARK_BINS)
        self.assertEqual(bins[-1], 2)                  # newest bin: ages 0-29
        self.assertEqual(bins[-2], 1)                  # age 45 is in 30-59
        self.assertEqual(bins[0], 1)                   # age 575 is in the oldest bin
        self.assertEqual(sum(bins), 4)

    def test_quiet_seconds_is_none_when_empty_then_grows(self):
        self.assertIsNone(self.feed.quiet_seconds())
        self.feed.add(msg(1, "hi", age=0))
        self.assertEqual(self.feed.quiet_seconds(), 0.0)
        self.clock.now += 42
        self.assertEqual(self.feed.quiet_seconds(), 42.0)
        self.clock.now += 100
        self.assertEqual(self.feed.quiet_seconds(), 142.0)

    def test_spike_against_a_quiet_baseline(self):
        self.add_aged([2, 7, 12, 17, 22, 27])   # six in 30 s, nothing before
        self.assertTrue(self.feed.spike())

    def test_five_in_thirty_seconds_is_not_a_spike(self):
        self.add_aged([2, 7, 12, 17, 22])
        self.assertFalse(self.feed.spike())

    def test_steadily_busy_chat_is_not_a_spike(self):
        # a message every 5 s for five and a half minutes: 6 recent, but 6 per bin before
        self.add_aged([2 + 5 * i for i in range(66)])
        self.assertGreaterEqual(self.feed._count(30.0), chatfeed.SPIKE_MIN_MESSAGES)
        self.assertFalse(self.feed.spike())


class TestTally(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.feed = ChatFeed(clock=self.clock)
        self.n = 0

    def say(self, slug, text, age=1.0):
        self.n += 1
        self.feed.add(msg("v{0}".format(self.n), text, slug=slug, age=age))

    def test_plain_and_punctuated_digits_count(self):
        self.say("a", "1")
        self.say("b", "1.")
        self.say("c", "2!")
        self.say("d", "2)")
        self.say("e", "3")
        self.assertEqual(self.feed.tally(3, 0), [2, 2, 1])

    def test_things_that_are_not_votes(self):
        for i, text in enumerate(["12", "1 please", "one", "10", "a1", "1 2"]):
            self.say("u{0}".format(i), text)
        self.assertEqual(self.feed.tally(3, 0), [0, 0, 0])

    def test_one_vote_per_chatter_latest_wins(self):
        self.say("a", "1", age=30)
        self.say("a", "2", age=20)
        self.say("a", "3", age=10)
        self.say("b", "1", age=5)
        self.assertEqual(self.feed.tally(3, 0), [1, 0, 1])

    def test_votes_before_since_are_ignored(self):
        self.say("a", "1", age=50)
        self.say("b", "2", age=5)
        self.assertEqual(self.feed.tally(2, NOW - 20), [0, 1])
        # an old vote does not survive as the "latest" either: a's only vote is too old
        self.say("a", "2", age=2)
        self.assertEqual(self.feed.tally(2, NOW - 20), [0, 2])

    def test_choices_above_the_option_count_are_ignored(self):
        self.say("a", "3")
        self.say("b", "2")
        self.say("c", "1")
        self.assertEqual(self.feed.tally(2, 0), [1, 1])

    def test_fewer_than_two_options_is_empty(self):
        self.say("a", "1")
        self.assertEqual(self.feed.tally(0, 0), [])
        self.assertEqual(self.feed.tally(1, 0), [])

    def test_options_are_capped_at_three(self):
        self.say("a", "3")
        self.assertEqual(self.feed.tally(9, 0), [0, 0, 1])


class TestQuestions(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.feed = ChatFeed(clock=self.clock)

    def test_looks_like_question(self):
        q = ChatFeed.looks_like_question
        self.assertTrue(q("is this ranked?"))
        self.assertTrue(q("kya setup hai tumhara"))
        self.assertTrue(q("how do you aim that well"))
        self.assertFalse(q("!specs"))
        self.assertFalse(q("1"))
        self.assertFalse(q("nice play"))
        self.assertFalse(q("how are"))          # a question word alone with one more is too short
        self.assertFalse(q("!what is this?"))   # commands are never questions

    def test_questions_newest_first_excluding_dismissed_and_limited(self):
        self.feed.add(msg("q1", "kya setup hai tumhara", slug="a", age=30))
        self.feed.add(msg("x", "nice shot", slug="b", age=25))
        self.feed.add(msg("q2", "which game next?", slug="c", age=20))
        self.feed.add(msg("q3", "how long have you played?", slug="d", age=10))
        qs = self.feed.questions()
        self.assertEqual([q["id"] for q in qs], ["q3", "q2", "q1"])
        self.assertEqual(qs[0]["age"], 10)
        self.assertEqual(set(qs[0]), {"id", "user", "text", "age"})
        self.feed.dismiss("q2")
        self.assertEqual([q["id"] for q in self.feed.questions()], ["q3", "q1"])
        self.assertEqual([q["id"] for q in self.feed.questions(limit=1)], ["q3"])

    def test_find(self):
        self.feed.add(msg("m1", "hello"))
        self.assertEqual(self.feed.find("m1")["text"], "hello")
        self.assertIsNone(self.feed.find("nope"))


class TestRepeats(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.feed = ChatFeed(clock=self.clock)
        self.n = 0

    def say(self, slug, text, age=10.0):
        self.n += 1
        self.feed.add(msg("r{0}".format(self.n), text, slug=slug, age=age))

    def test_three_different_chatters_are_listed_with_a_count(self):
        for slug in ("a", "b", "c", "d"):
            self.say(slug, "!discord")
        self.say("a", "!discord")
        rows = self.feed.repeats()
        self.assertEqual(rows, [{"text": "!discord", "chatters": 4}])

    def test_two_chatters_are_not_enough(self):
        self.say("a", "!discord")
        self.say("b", "!discord")
        self.assertEqual(self.feed.repeats(), [])

    def test_same_chatter_repeating_is_not_listed(self):
        for _ in range(5):
            self.say("spammer", "!specs")
        self.assertEqual(self.feed.repeats(), [])

    def test_case_and_spacing_are_folded_together(self):
        self.say("a", "Nice play")
        self.say("b", "nice   play")
        self.say("c", "NICE PLAY")
        rows = self.feed.repeats()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chatters"], 3)

    def test_vote_digits_are_excluded(self):
        for slug in ("a", "b", "c"):
            self.say(slug, "1")
        self.assertEqual(self.feed.repeats(), [])

    def test_long_messages_are_excluded(self):
        long_text = "this is a rather long message that goes on well past sixty characters in length"
        for slug in ("a", "b", "c"):
            self.say(slug, long_text)
        self.assertEqual(self.feed.repeats(), [])

    def test_old_messages_are_excluded(self):
        for slug in ("a", "b", "c"):
            self.say(slug, "!discord", age=chatfeed.REPEAT_WINDOW_SECONDS + 60)
        self.assertEqual(self.feed.repeats(), [])

    def test_most_repeated_first(self):
        for slug in ("a", "b", "c"):
            self.say(slug, "!discord")
        for slug in ("a", "b", "c", "d", "e"):
            self.say(slug, "!giveaway")
        self.assertEqual([r["text"] for r in self.feed.repeats()], ["!giveaway", "!discord"])


class TestSnapshot(unittest.TestCase):
    KEYS = {"rate", "spark", "quiet", "quietAlert", "spike", "total", "chatters", "questions", "repeats",
            "newcomers", "recent", "mood", "words"}

    def test_empty_snapshot(self):
        feed = ChatFeed(clock=Clock())
        snap = feed.snapshot()
        self.assertEqual(set(snap), self.KEYS)
        self.assertIsNone(snap["quiet"])
        self.assertFalse(snap["quietAlert"])
        self.assertEqual(snap["total"], 0)
        self.assertEqual(snap["rate"], 0.0)

    def test_quiet_alert_after_three_minutes_of_silence(self):
        clock = Clock()
        feed = ChatFeed(clock=clock)
        feed.add(msg(1, "hello?", slug="a", age=0))
        snap = feed.snapshot()
        self.assertEqual(set(snap), self.KEYS)
        self.assertFalse(snap["quietAlert"])
        self.assertEqual(snap["total"], 1)
        self.assertEqual(snap["chatters"], 1)
        self.assertEqual(snap["recent"], [{"id": "1", "user": "a", "text": "hello?"}])
        self.assertEqual(len(snap["questions"]), 1)
        clock.now += chatfeed.QUIET_ALERT_SECONDS
        snap = feed.snapshot()
        self.assertTrue(snap["quietAlert"])
        self.assertEqual(snap["quiet"], 180.0)

    def test_recent_returns_the_latest_in_order(self):
        feed = ChatFeed(clock=Clock())
        for i in range(12):
            feed.add(msg(i, "m{0}".format(i), slug="u{0}".format(i)))
        self.assertEqual([m["id"] for m in feed.recent(3)], ["9", "10", "11"])


if __name__ == "__main__":
    unittest.main()
