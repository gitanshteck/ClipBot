"""Links on brief cards (what the dock offers to open live): validation, the allow-list
of URLs the server will open, and POST /api/brief/open (clipbot/brief.py, server/app.py)."""

import unittest
from unittest import mock

from clipbot import brief
from test_brief import BriefTestCase, HEADERS, TestClient, app_module, sample_brief

LINK = {"label": "Reveal trailer", "url": "https://www.youtube.com/watch?v=abc123", "kind": "trailer"}


def with_links(links, where="docket"):
    b = sample_brief()
    if where == "docket":
        b["docket"][0]["links"] = links
    else:
        b["run"][1]["links"] = links
    return b


class TestLinkValidation(unittest.TestCase):
    def test_a_good_link_passes_on_an_item_and_on_a_segment(self):
        self.assertEqual(brief.problems(with_links([LINK])), [])
        self.assertEqual(brief.problems(with_links([LINK], "run")), [])

    def test_no_links_is_fine(self):
        self.assertEqual(brief.problems(sample_brief()), [])

    def test_bad_links_are_named(self):
        for links, fragment in (
            ("nope", "list of at most"),
            ([LINK] * 7, "list of at most"),
            ([{"label": "x", "url": "javascript:alert(1)"}], "http"),
            ([{"label": "x", "url": "ftp://example.com/a"}], "http"),
            ([{"label": "", "url": "https://example.com"}], "label"),
            ([{"label": "x" * 33, "url": "https://example.com"}], "label"),
            ([{"label": "x", "url": "https://example.com", "kind": "banana"}], "kind"),
            (["https://example.com"], "not an object"),
        ):
            with self.subTest(links=links):
                found = brief.problems(with_links(links))
                self.assertTrue(any(fragment in p for p in found), found)

    def test_a_non_http_item_url_is_refused(self):
        b = sample_brief()
        b["docket"][0]["url"] = "javascript:alert(1)"
        self.assertTrue(any("url must start with http" in p for p in brief.problems(b)))

    def test_link_urls_collects_everything_the_brief_offers(self):
        b = with_links([LINK])
        b["run"][1]["links"] = [{"label": "Seg", "url": "https://example.org/seg"}]
        b["sources"] = [{"label": "s", "url": "https://example.net/src"}]
        urls = brief.link_urls(b)
        self.assertEqual(urls, {LINK["url"], "https://example.org/seg", "https://example.net/src", "https://example.com"})
        self.assertEqual(brief.link_urls(None), set())


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestOpenRoute(BriefTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(app_module, "settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")
        app_module._brief_cache["mtime"] = None
        self.write_brief(with_links([LINK]))

    def test_opens_only_urls_the_brief_lists(self):
        with mock.patch.object(app_module.webbrowser, "open", return_value=True) as opener:
            ok = self.client.post("/api/brief/open", json={"url": LINK["url"]}, headers=HEADERS)
            self.assertEqual(ok.status_code, 200)
            opener.assert_called_once_with(LINK["url"])
            opener.reset_mock()
            item_url = self.client.post("/api/brief/open", json={"url": "https://example.com"}, headers=HEADERS)
            self.assertEqual(item_url.status_code, 200)
            opener.reset_mock()
            for bad in ("https://evil.example/x", "file:///c:/windows/system32/calc.exe", "javascript:alert(1)", ""):
                r = self.client.post("/api/brief/open", json={"url": bad}, headers=HEADERS)
                self.assertEqual(r.status_code, 400, bad)
            opener.assert_not_called()

    def test_needs_the_clipbot_header(self):
        with mock.patch.object(app_module.webbrowser, "open", return_value=True) as opener:
            self.assertEqual(self.client.post("/api/brief/open", json={"url": LINK["url"]}).status_code, 403)
            opener.assert_not_called()

    def test_no_brief_means_nothing_to_open(self):
        (self.dir / "brief.json").unlink()
        app_module._brief_cache["mtime"] = None
        with mock.patch.object(app_module.webbrowser, "open", return_value=True) as opener:
            self.assertEqual(self.client.post("/api/brief/open", json={"url": LINK["url"]}, headers=HEADERS).status_code, 400)
            opener.assert_not_called()
