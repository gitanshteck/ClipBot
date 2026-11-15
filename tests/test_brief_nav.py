"""Moving between ClipBot and Cue: the ClipBot topbar has a way into Cue, and the Cue pages have a way back."""

import unittest
from unittest import mock

from test_brief import BriefTestCase, TestClient, app_module


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestNavigation(BriefTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(app_module, "settings", self.settings)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")

    def test_clipbot_pages_link_to_cue(self):
        for path in ("/kick", "/youtube"):
            html = self.client.get(path).text
            self.assertIn('href="/brief"', html, path)

    def test_the_dock_links_back_to_clipbot(self):
        html = self.client.get("/brief/dock").text
        self.assertIn('href="/"', html)
        self.assertIn("ClipBot", html)

    def test_the_rundown_links_back_to_clipbot_only_when_served_locally(self):
        self.write_brief()
        local = self.client.get("/brief").text
        self.assertIn('class="xnav"', local)
        self.assertIn('href="/kick"', local)
        from clipbot import brief
        artifact = brief.render_page(brief.load_brief(self.settings), "claude")
        self.assertIn("if (LOCAL) h.push('<nav class=\"xnav\"", artifact)   # the code is there but only runs for the local page
        self.assertNotIn('"backend": "local"', artifact)


if __name__ == "__main__":
    unittest.main()
