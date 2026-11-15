"""Cue's live chat feed on the brief side: poll option counting, the pinned chat message
in the live state, chat context for the AI, and the /api/brief/feed* routes.

The Kick transport is replaced by a fake class, so nothing here reaches the network.
The vote loop itself is an infinite async task and is not run; its parts (ChatFeed.tally,
brief.options_for) are tested together instead.
"""

import time
import types
import unittest
from unittest import mock

from clipbot import brief, briefchat
from clipbot.chatfeed import ChatFeed

from test_brief import BriefTestCase, sample_brief

try:
    from fastapi.testclient import TestClient
    from clipbot.server import app as app_module
except Exception:  # pragma: no cover - server extras not installed
    TestClient = None

HEADERS = {"X-ClipBot": "1"}
DATE = "2026-11-14"


def state_for(seg, item=0, date=DATE, **over):
    s = brief.default_state(date)
    s.update({"v": 1, "on": True, "seg": seg, "item": item})
    s.update(over)
    return s


class TestOptionsFor(unittest.TestCase):
    def test_vote_scene_with_two_options(self):
        self.assertEqual(brief.options_for(sample_brief(), state_for(2)), 2)

    def test_three_item_poll_counts_three(self):
        b = sample_brief()
        b["run"][2]["poll"] = ["a", "b", "c"]
        self.assertEqual(brief.options_for(b, state_for(2)), 3)

    def test_polls_are_capped_at_three(self):
        b = sample_brief()
        b["run"][2]["poll"] = ["a", "b", "c", "d", "e"]
        self.assertEqual(brief.options_for(b, state_for(2)), 3)

    def test_topic_card_whose_current_docket_item_has_a_poll(self):
        b = sample_brief()
        b["docket"][0]["poll"] = ["yes", "no"]
        self.assertEqual(brief.options_for(b, state_for(1)), 2)

    def test_card_without_options(self):
        self.assertEqual(brief.options_for(sample_brief(), state_for(1)), 0)   # topic, no poll on its item
        self.assertEqual(brief.options_for(sample_brief(), state_for(3)), 0)   # game
        self.assertEqual(brief.options_for(sample_brief(), state_for(0)), 0)   # starting

    def test_state_from_another_date_has_none(self):
        self.assertEqual(brief.options_for(sample_brief(), state_for(2, date="2026-11-13")), 0)

    def test_a_brief_with_no_run_has_none(self):
        self.assertEqual(brief.options_for(sample_brief(run=[]), state_for(2)), 0)


class TestVotesEndToEnd(unittest.TestCase):
    """What the vote loop does each second, minus the sleeping."""

    def test_tally_uses_the_card_start_and_the_option_count(self):
        now = time.time()
        feed = ChatFeed(clock=lambda: now)
        opts = brief.options_for(sample_brief(), state_for(2))
        card_started = now - 30

        def say(i, slug, text, age):
            feed.add({"id": "m{0}".format(i), "user": slug, "slug": slug, "text": text, "at": now - age})

        say(1, "early", "2", 120)      # voted before this card came on screen
        say(2, "a", "1", 20)
        say(3, "b", "2", 15)
        say(4, "c", "2", 10)
        say(5, "d", "3", 5)            # there is no third option
        say(6, "a", "2", 3)            # a changes their mind
        self.assertEqual(opts, 2)
        self.assertEqual(feed.tally(opts, card_started), [0, 3])
        self.assertEqual(feed.tally(opts, 0), [0, 4])                  # "early" counts when nothing is cut off
        self.assertEqual(feed.tally(brief.options_for(sample_brief(), state_for(3)), card_started), [])   # not a vote card


class TestPinState(BriefTestCase):
    def test_default_state_has_no_pin(self):
        self.assertIsNone(brief.default_state("d")["pin"])

    def write(self, **over):
        s = state_for(1)
        s.update(over)
        return brief.write_state(self.settings, s)

    def test_a_pin_is_stored_and_truncated(self):
        stored = self.write(pin={"u": "u" * 60, "t": "t" * 500, "id": "m1"})
        self.assertEqual(stored["pin"], {"u": "u" * 40, "t": "t" * 300, "id": "m1"})
        self.assertEqual(brief.read_state(self.settings, DATE)["pin"], stored["pin"])

    def test_a_pin_without_an_id_gets_an_empty_one(self):
        self.assertEqual(self.write(pin={"u": "a", "t": "b"})["pin"], {"u": "a", "t": "b", "id": ""})

    def test_none_clears_the_pin(self):
        self.write(pin={"u": "a", "t": "b", "id": "1"})
        stored = brief.write_state(self.settings, dict(state_for(1), v=2, pin=None))
        self.assertIsNone(stored["pin"])

    def test_bad_pins_are_refused(self):
        for bad in ("hello", ["a", "b"], 3, {"u": "a"}, {"t": "b"}, {"u": 1, "t": "b"}, {"u": "a", "t": None}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.write(pin=bad)


class TestStreamContextChat(unittest.TestCase):
    CHAT = {"rate": 12, "recent": [{"user": "a", "text": "hi"}]}

    def test_chat_is_included_without_a_brief(self):
        text = briefchat.stream_context(None, None, "", self.CHAT)
        self.assertIn("12 messages a minute", text)
        self.assertIn("a: hi", text)

    def test_chat_is_included_with_a_brief(self):
        text = briefchat.stream_context(sample_brief(), state_for(1), "dock", self.CHAT)
        self.assertIn("12 messages a minute", text)
        self.assertIn("a: hi", text)
        self.assertIn("Tonight's theme: k", text)

    def test_no_chat_means_no_chat_line(self):
        self.assertNotIn("messages a minute", briefchat.stream_context(None, None, ""))
        self.assertNotIn("messages a minute", briefchat.stream_context(sample_brief(), state_for(1), ""))


class FakePoller(object):
    instances = []

    def __init__(self, channel, feed, interval=3.0, impersonate="chrome", **kw):
        self.channel = channel
        self.feed = feed
        self.interval = interval
        self.status = "live"
        self.started = False
        self.stopped = False
        FakePoller.instances.append(self)

    def state(self):
        return {"status": self.status, "channel": self.channel, "error": "", "last_ok_age": 0.0}

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.status = "stopped"


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestFeedRoutes(BriefTestCase):
    def setUp(self):
        BriefTestCase.setUp(self)
        FakePoller.instances = []
        for patcher in (mock.patch.object(app_module, "settings", self.settings),
                        mock.patch.object(app_module.kickchat, "KickPoller", FakePoller),
                        mock.patch.object(app_module, "_feed", None),
                        mock.patch.object(app_module, "_kick", None),
                        mock.patch.dict(app_module._vote, {"on": True, "card": None, "since": 0.0, "last": []}),
                        mock.patch.dict(app_module._feed_last, {"hash": ""}),
                        mock.patch.dict(app_module._brief_cache, {"mtime": None, "data": None})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")

    def start_feed(self, channel="teckstream"):
        r = self.client.post("/api/brief/feed/start", json={"channel": channel}, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def add(self, mid, text, user="viewer", age=1.0):
        self.assertTrue(app_module._feed.add({"id": mid, "user": user, "slug": user, "text": text, "at": time.time() - age}))

    # -- connection ---------------------------------------------------------

    def test_feed_is_off_until_started(self):
        body = self.client.get("/api/brief/feed").json()
        self.assertEqual(body["connection"]["status"], "off")
        self.assertIs(body["votesFromChat"], True)
        self.assertNotIn("rate", body)

    def test_start_needs_the_header(self):
        r = self.client.post("/api/brief/feed/start", json={"channel": "teckstream"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(app_module._kick)

    def test_start_rejects_an_invalid_channel(self):
        for channel in ("", "a", "has space", "bad/channel", "x" * 41, "../etc"):
            r = self.client.post("/api/brief/feed/start", json={"channel": channel}, headers=HEADERS)
            self.assertEqual(r.status_code, 400, channel)
        self.assertEqual(FakePoller.instances, [])

    def test_start_with_a_valid_channel_goes_live(self):
        body = self.start_feed("TeckStream")
        self.assertEqual(body["connection"]["status"], "live")
        self.assertEqual(body["connection"]["channel"], "teckstream")
        self.assertIsInstance(app_module._feed, ChatFeed)
        self.assertEqual(len(FakePoller.instances), 1)
        self.assertTrue(FakePoller.instances[0].started)
        self.assertIs(FakePoller.instances[0].feed, app_module._feed)
        self.assertIn("rate", body)
        self.assertIn("spark", body)
        self.assertEqual(self.client.get("/api/brief/feed").json()["connection"]["status"], "live")

    def test_starting_again_stops_the_previous_poller(self):
        self.start_feed()
        self.start_feed("otherchannel")
        first, second = FakePoller.instances
        self.assertTrue(first.stopped)
        self.assertFalse(second.stopped)

    def test_stop_turns_it_off(self):
        self.start_feed()
        poller = FakePoller.instances[0]
        r = self.client.post("/api/brief/feed/stop", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["connection"]["status"], "off")
        self.assertTrue(poller.stopped)
        self.assertIsNone(app_module._kick)
        self.assertEqual(self.client.get("/api/brief/feed").json()["connection"]["status"], "off")

    def test_stop_when_never_started_is_fine(self):
        r = self.client.post("/api/brief/feed/stop", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["connection"]["status"], "off")

    # -- votes, dismiss -----------------------------------------------------

    def test_votes_toggle(self):
        r = self.client.post("/api/brief/feed/votes", json={"on": False}, headers=HEADERS)
        self.assertEqual(r.json(), {"votesFromChat": False})
        self.assertIs(self.client.get("/api/brief/feed").json()["votesFromChat"], False)
        r = self.client.post("/api/brief/feed/votes", json={"on": True}, headers=HEADERS)
        self.assertEqual(r.json(), {"votesFromChat": True})
        self.assertEqual(self.client.post("/api/brief/feed/votes", json={"on": False}).status_code, 403)

    def test_dismiss_removes_a_question(self):
        self.start_feed()
        self.add("q1", "which game is this?", user="a")
        self.add("q2", "kya setup hai tumhara", user="b")
        ids = [q["id"] for q in self.client.get("/api/brief/feed").json()["questions"]]
        self.assertEqual(ids, ["q2", "q1"])
        r = self.client.post("/api/brief/feed/dismiss", json={"id": "q2"}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        ids = [q["id"] for q in self.client.get("/api/brief/feed").json()["questions"]]
        self.assertEqual(ids, ["q1"])

    def test_dismiss_without_a_feed_is_harmless(self):
        r = self.client.post("/api/brief/feed/dismiss", json={"id": "x"}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)

    # -- pin ----------------------------------------------------------------

    def test_pin_writes_the_state_and_clearing_removes_it(self):
        self.write_brief()
        self.start_feed()
        self.add("m1", "is this ranked?", user="viewer")
        r = self.client.post("/api/brief/feed/pin", json={"id": "m1"}, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pin"], {"u": "viewer", "t": "is this ranked?", "id": "m1"})
        state = self.client.get("/api/brief/state").json()
        self.assertEqual(state["pin"], {"u": "viewer", "t": "is this ranked?", "id": "m1"})
        self.assertEqual(state["date"], DATE)
        r = self.client.post("/api/brief/feed/pin", json={}, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.client.get("/api/brief/state").json()["pin"])

    def test_pin_keeps_the_rest_of_the_state(self):
        self.write_brief()
        self.start_feed()
        self.add("m1", "hello?")
        brief.write_state(self.settings, state_for(2, v=5, kills=3))
        self.client.post("/api/brief/feed/pin", json={"id": "m1"}, headers=HEADERS)
        state = self.client.get("/api/brief/state").json()
        self.assertEqual((state["seg"], state["kills"], state["on"]), (2, 3, True))
        self.assertGreater(state["v"], 5)

    def test_pin_of_an_unknown_message_is_404(self):
        self.write_brief()
        self.start_feed()
        r = self.client.post("/api/brief/feed/pin", json={"id": "gone"}, headers=HEADERS)
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(self.client.get("/api/brief/state").json()["pin"])

    def test_pin_without_a_feed_is_404(self):
        self.write_brief()
        r = self.client.post("/api/brief/feed/pin", json={"id": "m1"}, headers=HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_pin_needs_the_header(self):
        self.assertEqual(self.client.post("/api/brief/feed/pin", json={"id": "m1"}).status_code, 403)

    # -- chat context for the AI --------------------------------------------

    def ask(self):
        captured = {}

        def fake_ask(settings, ask_obj, prompt, on_delta):
            captured["prompt"] = prompt
            ask_obj.done = True
            return ask_obj

        class SyncThread(object):
            def __init__(self, target=None, args=(), daemon=None, **kw):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with mock.patch.object(app_module.briefchat, "pick_provider", return_value="anthropic"), \
                mock.patch.object(app_module.briefchat, "ask", side_effect=fake_ask), \
                mock.patch.object(app_module, "threading", types.SimpleNamespace(Thread=SyncThread)):
            r = self.client.post("/api/brief/chat", json={"question": "what is chat asking?", "page": "dock"}, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        return captured["prompt"]

    def test_the_ai_sees_live_chat(self):
        self.write_brief()
        self.start_feed()
        self.add("m1", "is this ranked?", user="viewer")
        self.add("m2", "gg", user="friend")
        prompt = self.ask()
        self.assertIn("messages a minute", prompt)
        self.assertIn("viewer: is this ranked?", prompt)
        self.assertIn("friend: gg", prompt)
        self.assertIn("Tonight's theme: k", prompt)

    def test_the_ai_does_not_see_chat_when_the_feed_is_off(self):
        self.write_brief()
        self.assertNotIn("messages a minute", self.ask())

    def test_the_ai_does_not_see_chat_when_the_connection_is_down(self):
        self.write_brief()
        self.start_feed()
        self.add("m1", "is this ranked?")
        FakePoller.instances[0].status = "error"
        self.assertNotIn("is this ranked?", self.ask())


if __name__ == "__main__":
    unittest.main()
