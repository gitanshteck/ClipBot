"""The dashboard side of the +1 tally, the widgets and AI moments (clipbot/server/app.py), plus the
chat-feed additions they rest on (clipbot/chatfeed.py)."""

import json
import random
import time
import unittest
from unittest import mock

from clipbot import aimoments, chatfeed
from test_brief import BriefTestCase, HEADERS, TestClient, app_module, sample_brief

DATE = "2026-11-14"


def feed_with(*rows, clock=None):
    feed = chatfeed.ChatFeed(clock=clock or time.time)
    for i, (user, text) in enumerate(rows):
        feed.add({"id": "m%d" % i, "user": user, "slug": user.lower(), "text": text, "at": (clock or time.time)()})
    return feed


class TestChatFeedAdditions(unittest.TestCase):
    def test_plus_and_minus_one_chatter_one_vote_latest_wins(self):
        feed = feed_with(("a", "+1"), ("b", "+1"), ("c", "++"), ("a", "-1"), ("d", "−1"), ("e", "+ 1"), ("f", "1"), ("g", "+1 lol"))
        self.assertEqual(feed.plus(0), [3, 2])          # b, c, e up; a, d down; "1" and "+1 lol" are not tally messages

    def test_plus_counts_only_since_the_start_time(self):
        t = [100.0]
        feed = chatfeed.ChatFeed(clock=lambda: t[0])
        feed.add({"id": "1", "user": "a", "slug": "a", "text": "+1", "at": 100.0})
        feed.add({"id": "2", "user": "b", "slug": "b", "text": "+1", "at": 200.0})
        self.assertEqual(feed.plus(150.0), [1, 0])

    def test_plus_messages_are_not_questions_or_repeats(self):
        feed = feed_with(("a", "+1"), ("b", "+1"), ("c", "+1"), ("d", "-1"))
        self.assertEqual(feed.repeats(), [])
        self.assertEqual(feed.questions(), [])

    def test_words_leave_out_votes_commands_and_noise(self):
        feed = feed_with(("a", "that clutch was insane"), ("b", "insane clutch bro"), ("c", "!specs"), ("d", "1"), ("e", "+1"))
        words = {w["w"]: w["n"] for w in feed.words()}
        self.assertEqual(words, {"clutch": 2, "insane": 2})

    def test_mood_is_zero_without_chat_and_higher_when_chat_laughs(self):
        self.assertEqual(chatfeed.ChatFeed().mood(), 0)
        quiet = feed_with(*[("u%d" % i, "ok") for i in range(3)])
        loud = feed_with(*[("u%d" % i, "lol that was insane") for i in range(12)])
        self.assertGreater(loud.mood(), quiet.mood())
        self.assertLessEqual(loud.mood(), 100)

    def test_the_sink_sees_accepted_messages_and_cannot_break_the_feed(self):
        seen = []
        feed = chatfeed.ChatFeed(sink=seen.append)
        feed.add({"id": "1", "user": "a", "slug": "a", "text": "hello", "at": 1.0})
        feed.add({"id": "1", "user": "a", "slug": "a", "text": "hello", "at": 1.0})   # duplicate: not sunk twice
        self.assertEqual([r["text"] for r in seen], ["hello"])

        def boom(row):
            raise RuntimeError("disk full")
        broken = chatfeed.ChatFeed(sink=boom)
        self.assertTrue(broken.add({"id": "1", "user": "a", "slug": "a", "text": "hello", "at": 1.0}))
        self.assertEqual(broken.total, 1)


class FakeKick(object):
    status = "live"

    def state(self):
        return {"status": "live", "channel": "x", "error": "", "last_ok_age": 0.5}

    def stop(self):
        pass


class Gen(object):
    def __init__(self, *lines):
        self.lines = list(lines)

    def __call__(self, settings, prompt):
        return (self.lines.pop(0) if self.lines else "SKIP"), "fake"


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestRoutes(BriefTestCase):
    def setUp(self):
        super().setUp()
        for target, value in (("settings", self.settings), ("_kick", FakeKick()),
                              ("_feed", feed_with(("a", "+1"), ("b", "+1"), ("c", "-1"), ("d", "lol"))),
                              ("_engine", aimoments.Engine(self.settings, Gen("chat is cooking harder than my dinner"), time.time, random.Random(1)))):
            p = mock.patch.object(app_module, target, value)
            p.start()
            self.addCleanup(p.stop)
        app_module._callout.update(rec=None, until=0.0)
        app_module._vote.update(on=True, since=0.0, last=[], card=None)
        app_module._brief_cache["mtime"] = None
        self.write_brief()
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")
        pub = mock.patch.object(app_module.bus, "publish")
        self.publish = pub.start()
        self.addCleanup(pub.stop)

    def published(self, key):
        return [c.args[1][key] for c in self.publish.call_args_list if c.args[1].get(key)]

    def test_widgets_carry_the_plus_tally_and_the_mood(self):
        w = self.client.get("/api/brief/widgets").json()
        self.assertEqual(w["plus"], [2, 1])
        self.assertGreater(w["mood"], 0)
        self.assertIsNone(w["callout"])

    def test_plus_is_zero_when_counting_is_off_or_the_feed_is_down(self):
        app_module._vote["on"] = False
        self.assertEqual(self.client.get("/api/brief/widgets").json()["plus"], [0, 0])
        app_module._vote["on"] = True
        with mock.patch.object(app_module, "_kick", None):
            self.assertEqual(self.client.get("/api/brief/widgets").json()["plus"], [0, 0])

    def test_pulse_reset_counts_again_from_now(self):
        self.assertEqual(self.client.post("/api/brief/pulse/reset", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/brief/pulse/reset", json={}, headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.get("/api/brief/widgets").json()["plus"], [0, 0])

    def test_settings_need_the_header_and_validate(self):
        self.assertEqual(self.client.put("/api/brief/moments/settings", json={"enabled": True}).status_code, 403)
        self.assertEqual(self.client.put("/api/brief/moments/settings", json={"bogus": 1}, headers=HEADERS).status_code, 400)
        ok = self.client.put("/api/brief/moments/settings", json={"enabled": True}, headers=HEADERS)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["status"]["config"]["enabled"])

    def test_fire_makes_a_pending_moment_that_is_logged_and_published(self):
        r = self.client.post("/api/brief/moments/fire", json={}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        rec = r.json()
        self.assertEqual((rec["kind"], rec["status"]), ("random", "pending"))
        self.assertEqual(self.published("moment")[0]["id"], rec["id"])
        listed = self.client.get("/api/brief/moments?date=" + DATE).json()
        self.assertEqual([m["id"] for m in listed["moments"]], [rec["id"]])
        self.assertEqual(listed["status"]["used_today"], 1)

    def test_fire_needs_a_live_feed_and_a_known_kind(self):
        self.assertEqual(self.client.post("/api/brief/moments/fire", json={"kind": "spike"}, headers=HEADERS).status_code, 400)
        with mock.patch.object(app_module, "_kick", None):
            self.assertEqual(self.client.post("/api/brief/moments/fire", json={}, headers=HEADERS).status_code, 409)

    def test_veto_and_reshow_work_and_report_unknown_ids(self):
        rec = self.client.post("/api/brief/moments/fire", json={}, headers=HEADERS).json()
        self.assertEqual(self.client.post("/api/brief/moments/%s/veto" % rec["id"], json={}, headers=HEADERS).status_code, 200)
        self.assertEqual(aimoments.find(self.settings, DATE, rec["id"])["status"], "vetoed")
        self.assertEqual(self.client.post("/api/brief/moments/%s/veto" % rec["id"], json={}, headers=HEADERS).status_code, 404)
        self.assertEqual(self.client.post("/api/brief/moments/%s/reshow" % rec["id"], json={}, headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.get("/api/brief/widgets").json()["callout"]["text"], "chat is cooking harder than my dinner")
        self.assertEqual(self.client.post("/api/brief/moments/nope/reshow", json={}, headers=HEADERS).status_code, 404)

    def test_a_callout_clears_itself_after_its_time(self):
        rec = self.client.post("/api/brief/moments/fire", json={}, headers=HEADERS).json()
        self.client.post("/api/brief/moments/%s/reshow" % rec["id"], json={}, headers=HEADERS)
        app_module._callout["until"] = time.time() - 1
        self.assertIsNone(self.client.get("/api/brief/widgets").json()["callout"])

    def test_shoutout_welcomes_a_name_and_strips_anything_odd(self):
        self.assertEqual(self.client.post("/api/brief/shoutout", json={"user": "Aarav"}).status_code, 403)
        ok = self.client.post("/api/brief/shoutout", json={"user": "<b>Aarav_99</b>"}, headers=HEADERS)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.get("/api/brief/widgets").json()["callout"]["text"], "welcome to the chat, bAarav_99b")
        self.assertEqual(self.client.post("/api/brief/shoutout", json={"user": "  "}, headers=HEADERS).status_code, 400)

    def test_every_accepted_chat_message_lands_in_the_days_log(self):
        app_module._chat_sink({"at": time.time(), "user": "Aarav", "slug": "aarav", "text": "namaste नमस्ते"})
        files = list((self.dir / "chatlogs").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        row = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((row["user"], row["text"]), ("Aarav", "namaste नमस्ते"))


if __name__ == "__main__":
    unittest.main()
