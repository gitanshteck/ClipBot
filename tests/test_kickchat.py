"""clipbot/kickchat.py: the read-only Kick chat transport, driven by a fake http object.

Nothing here touches the network; time is injected for poll_once and BACKOFF is
shrunk for the thread test.
"""

import calendar
import re
import threading
import time
import unittest
from unittest import mock

from clipbot import kickchat
from clipbot.chatfeed import ChatFeed

NOW = float(calendar.timegm(time.strptime("2026-11-09T11:55:00Z", "%Y-%m-%dT%H:%M:%SZ")))


def stamp(age):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age))


def raw(mid, text, age, slug="viewer", username="Viewer"):
    return {"id": mid, "content": text, "created_at": stamp(age), "type": "message",
            "sender": {"slug": slug, "username": username, "identity": {"badges": []}}}


class FakeResponse(object):
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeHttp(object):
    """Answers the channel lookup with an id and the messages call with whatever ``pages`` holds."""

    def __init__(self, messages=None, channel_id=4242):
        self.messages = messages or []   # newest first, as Kick returns them
        self.channel_id = channel_id
        self.calls = []
        self.fail_status = None

    def get(self, url, impersonate=None, timeout=None):
        self.calls.append({"url": url, "impersonate": impersonate, "timeout": timeout})
        if self.fail_status:
            return FakeResponse(self.fail_status)
        if url.endswith("/messages") or "/messages?" in url:
            return FakeResponse(200, {"data": {"messages": list(self.messages)}})
        return FakeResponse(200, {"id": self.channel_id})


def make(http, **kw):
    feed = ChatFeed(clock=lambda: NOW)
    poller = kickchat.KickPoller("TeckStream", feed, http=http, clock=lambda: NOW, **kw)
    return poller, feed


class TestParseMessage(unittest.TestCase):
    def test_maps_a_realistic_message(self):
        message = kickchat.parse_message({
            "id": "abc-123", "chatroom_id": 9, "content": "  that was clean  ", "type": "message",
            "created_at": "2026-11-09T11:53:06Z",
            "sender": {"id": 7, "slug": "Some-Viewer", "username": "Some Viewer",
                       "identity": {"color": "#fff", "badges": [{"type": "moderator", "text": "Moderator"}]}}})
        self.assertEqual(message["id"], "abc-123")
        self.assertEqual(message["text"], "that was clean")
        self.assertEqual(message["user"], "Some Viewer")
        self.assertEqual(message["slug"], "some-viewer")
        self.assertEqual(message["badges"], ["moderator"])
        self.assertEqual(message["at"], float(calendar.timegm(time.strptime("2026-11-09T11:53:06Z", "%Y-%m-%dT%H:%M:%SZ"))))

    def test_none_without_an_id_or_text(self):
        sender = {"slug": "a", "username": "A"}
        self.assertIsNone(kickchat.parse_message({"content": "hi", "sender": sender}))
        self.assertIsNone(kickchat.parse_message({"id": "1", "content": "   ", "sender": sender}))
        self.assertIsNone(kickchat.parse_message({"id": "1", "sender": sender}))

    def test_a_missing_sender_does_not_crash(self):
        message = kickchat.parse_message({"id": "1", "content": "hi", "created_at": "2026-11-09T11:53:06Z"})
        self.assertEqual(message["user"], "?")
        self.assertEqual(message["slug"], "")
        self.assertEqual(message["badges"], [])


class TestPollOnce(unittest.TestCase):
    def test_resolves_then_asks_for_messages_with_a_cursor(self):
        http = FakeHttp([raw("1", "hello", 10)])
        poller, _ = make(http)
        poller.poll_once()
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0]["url"], "https://kick.com/api/v2/channels/teckstream")
        self.assertEqual(http.calls[0]["impersonate"], "chrome")
        self.assertIsNotNone(http.calls[0]["timeout"])
        url = http.calls[1]["url"]
        self.assertTrue(url.startswith("https://kick.com/api/v2/channels/4242/messages?cursor="), url)
        self.assertRegex(url, r"cursor=\d+$")
        self.assertEqual(int(re.search(r"cursor=(\d+)", url).group(1)), int(NOW * 1e6) + 5000000)
        self.assertEqual(poller.channel_id, 4242)
        self.assertEqual(poller.status, "live")
        self.assertEqual(poller.error, "")

    def test_the_channel_is_resolved_only_once(self):
        http = FakeHttp([])
        poller, _ = make(http)
        poller.poll_once()
        poller.poll_once()
        self.assertEqual(len(http.calls), 3)   # resolve + two message calls

    def test_first_poll_marks_old_messages_seen_without_adding_them(self):
        # Kick answers newest first
        http = FakeHttp([raw("3", "fresh two", 20, slug="b"), raw("2", "fresh one", 50, slug="a"),
                         raw("1", "old", 600, slug="c")])
        poller, feed = make(http)
        added = poller.poll_once()
        self.assertEqual(added, 2)
        self.assertEqual([m["id"] for m in feed.messages], ["2", "3"])   # oldest first
        self.assertIn("1", feed._ids)                                      # remembered, not shown
        self.assertEqual(feed.total, 2)
        self.assertEqual(feed.newcomer_list(), [])                         # the seed batch is not "new people"

    def test_second_poll_does_not_re_add(self):
        http = FakeHttp([raw("2", "fresh", 20), raw("1", "old", 600, slug="c")])
        poller, feed = make(http)
        self.assertEqual(poller.poll_once(), 1)
        self.assertEqual(poller.poll_once(), 0)
        self.assertEqual(len(feed.messages), 1)

    def test_later_polls_add_new_messages_and_count_newcomers(self):
        http = FakeHttp([raw("1", "first", 20, slug="a")])
        poller, feed = make(http)
        poller.poll_once()
        http.messages = [raw("2", "newer", 5, slug="b"), raw("1", "first", 20, slug="a")]
        self.assertEqual(poller.poll_once(), 1)
        self.assertEqual([m["id"] for m in feed.messages], ["1", "2"])
        self.assertEqual(feed.newcomer_list(), ["Viewer"])   # b is new after the seed poll; a came in with it

    def test_a_non_200_raises(self):
        http = FakeHttp()
        http.fail_status = 403
        poller, _ = make(http)
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            poller.poll_once()

    def test_resolve_without_an_integer_id_names_the_channel(self):
        for body in ({}, {"id": "abc"}, {"message": "not found"}):
            http = FakeHttp()
            http.get = lambda url, impersonate=None, timeout=None, body=body: FakeResponse(200, body)
            poller, _ = make(http)
            with self.assertRaisesRegex(RuntimeError, "teckstream"):
                poller.resolve()

    def test_poll_once_after_stop_leaves_status_stopped(self):
        http = FakeHttp([raw("1", "hi", 5)])
        poller, _ = make(http)
        poller.stop()
        self.assertEqual(poller.status, "stopped")
        poller.poll_once()
        self.assertEqual(poller.status, "stopped")

    def test_state_reports_age_of_last_success(self):
        http = FakeHttp([])
        poller, _ = make(http)
        self.assertIsNone(poller.state()["last_ok_age"])
        poller.poll_once()
        state = poller.state()
        self.assertEqual(state["status"], "live")
        self.assertEqual(state["channel"], "teckstream")
        self.assertEqual(state["last_ok_age"], 0.0)

    def test_interval_has_a_floor(self):
        poller, _ = make(FakeHttp(), interval=0.01)
        self.assertGreaterEqual(poller.interval, 1.5)


class FlakyHttp(FakeHttp):
    """Fails the first ``failures`` calls with HTTP 500, then behaves."""

    def __init__(self, failures, **kw):
        FakeHttp.__init__(self, **kw)
        self.failures = failures
        self.poller = None
        self.seen_status = []

    def get(self, url, impersonate=None, timeout=None):
        self.seen_status.append(self.poller.status)
        if len(self.calls) < self.failures:
            self.calls.append({"url": url})
            return FakeResponse(500)
        return FakeHttp.get(self, url, impersonate, timeout)


class TestRunLoop(unittest.TestCase):
    def wait_for(self, predicate, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_errors_then_recovers_then_stops(self):
        http = FlakyHttp(2, messages=[raw("1", "hello", 5)])
        poller, feed = make(http)
        http.poller = poller
        with mock.patch.object(kickchat, "BACKOFF", (0.01,)):
            poller.start()
            self.assertTrue(self.wait_for(lambda: poller.status == "live"), poller.state())
            # the first two calls failed, so by the third the poller was in "error" with the reason recorded
            self.assertEqual(http.seen_status[0], "connecting")
            self.assertEqual(http.seen_status[1], "error")
            self.assertEqual(http.seen_status[2], "error")
            self.assertEqual(poller.error, "")
            self.assertTrue(self.wait_for(lambda: len(feed.messages) == 1))
            poller.stop()
            poller.join(timeout=5)
        self.assertFalse(poller.is_alive())
        self.assertEqual(poller.status, "stopped")

    def test_error_text_is_kept_while_failing(self):
        http = FakeHttp()
        http.fail_status = 429
        poller, _ = make(http)
        with mock.patch.object(kickchat, "BACKOFF", (0.01,)):
            poller.start()
            self.assertTrue(self.wait_for(lambda: poller.status == "error"))
            self.assertIn("429", poller.error)
            poller.stop()
            poller.join(timeout=5)
        self.assertFalse(poller.is_alive())


if __name__ == "__main__":
    unittest.main()
