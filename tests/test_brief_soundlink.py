"""The sound source link: ping/status and "play on stream" (clipbot/server/app.py)."""

import unittest
from unittest import mock

from test_brief import BriefTestCase, HEADERS, TestClient, app_module


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestSoundLink(BriefTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(app_module, "settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")
        app_module._sound_ping["at"] = 0.0

    def test_not_connected_until_the_source_pings(self):
        st = self.client.get("/api/brief/sounds/status").json()
        self.assertEqual(st, {"connected": False, "age_seconds": None})
        self.assertEqual(self.client.post("/api/brief/sounds/ping", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/brief/sounds/ping", json={}, headers=HEADERS).status_code, 200)
        st = self.client.get("/api/brief/sounds/status").json()
        self.assertTrue(st["connected"])
        self.assertLess(st["age_seconds"], 5)

    def test_a_stale_ping_reads_as_not_connected(self):
        app_module._sound_ping["at"] = app_module.time.time() - (app_module.SOUND_PING_FRESH_SECONDS + 5)
        self.assertFalse(self.client.get("/api/brief/sounds/status").json()["connected"])

    def test_play_publishes_a_brief_event_for_known_events_only(self):
        with mock.patch.object(app_module.bus, "publish") as publish:
            ok = self.client.post("/api/brief/sounds/play", json={"event": "reveal"}, headers=HEADERS)
            self.assertEqual(ok.status_code, 200)
            publish.assert_called_once_with("brief", {"play": "reveal"})
            publish.reset_mock()
            self.assertEqual(self.client.post("/api/brief/sounds/play", json={"event": "boom"}, headers=HEADERS).status_code, 400)
            self.assertEqual(self.client.post("/api/brief/sounds/play", json={"event": "reveal"}).status_code, 403)
            publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
