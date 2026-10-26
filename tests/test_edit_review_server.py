"""The edit review page's API and media route (FastAPI test client, no startup hooks - the app's job threads never run)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from clipbot.server import app as app_module
except Exception as exc:  # pragma: no cover - server extras not installed
    TestClient = None
    _IMPORT_ERROR = exc

from clipbot import editreview
from clipbot.config import Settings

HEADERS = {"X-ClipBot": "1"}
VIDEO = bytes(range(256)) * 8                      # 2048 bytes: enough to ask for a range of


def timeline():
    def clip(cid, start, dur, tags=(), label=None, typ="clip"):
        return {"id": cid, "type": typ, "start": start, "dur": dur, "tags": list(tags), "label": label or cid.upper(),
                "src": "v" if typ == "clip" else None, "in": 100.0, "out": 100.0 + dur, "markers": []}

    return {"clips": [clip("sting", 0.0, 12.0, typ="card", label="Intro"), clip("a", 12.0, 20.0, ["chapter"], "Headshots"),
                      clip("b", 32.0, 10.0)], "duration": 42.0}


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestEditReviewApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.root = self.work / "_edits" / "e1"
        (self.root / "render" / "v1").mkdir(parents=True)
        (self.root / "spec.json").write_text("{}", encoding="utf-8")
        for rel, body in (("e1.preview.mp4", VIDEO), ("e1.mp4", VIDEO + b"x"), ("v1/e1.v1.mp4", VIDEO + b"yy")):
            (self.root / "render" / rel).write_bytes(body)
        (self.root / "render" / "e1.preview.timeline.json").write_text(json.dumps({"timeline": timeline(), "report": {"duration": 42.0}}), encoding="utf-8")
        (self.root / "render" / "e1.final.timeline.json").write_text(json.dumps({"timeline": timeline(), "report": {"duration": 42.0}}), encoding="utf-8")
        (self.root / "captions.json").write_text(json.dumps({"clips": {"a": {"cues": [{"text": "hello there"}, {"text": "hidden", "off": True}]}}}), encoding="utf-8")
        self.settings = Settings({"work_root": str(self.work)}, Path("settings.json"))
        patcher = mock.patch.object(app_module, "settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")

    def post(self, body, path="/api/edits/e1/comments"):
        return self.client.post(path, json=body, headers=HEADERS)

    # -- pages and the list --------------------------------------------------

    def test_the_pages_render(self):
        self.assertEqual(self.client.get("/edits").status_code, 200)
        r = self.client.get("/edits/e1/review")
        self.assertEqual(r.status_code, 200)
        self.assertIn('window.EDIT_NAME = "e1"', r.text)
        self.assertEqual(self.client.get("/edits/nope/review").status_code, 404)

    def test_the_list_counts_versions_and_open_comments(self):
        self.post({"t": 5, "text": "one"})
        c = self.post({"t": 6, "text": "two"}).json()
        self.client.patch("/api/edits/e1/comments/" + c["id"], json={"status": "done"}, headers=HEADERS)
        e = self.client.get("/api/edits").json()["edits"][0]
        self.assertEqual((e["name"], e["versions"], e["open_comments"], e["comments"], e["live"]), ("e1", ["v1", "final", "preview"], 1, 2, 2))

    def test_an_edit_folder_without_a_spec_or_render_is_not_listed(self):
        (self.work / "_edits" / "empty").mkdir()
        self.assertEqual([e["name"] for e in self.client.get("/api/edits").json()["edits"]], ["e1"])

    # -- the review payload ----------------------------------------------------

    def test_review_payload_has_versions_chapters_clips_and_the_captions_that_show(self):
        d = self.client.get("/api/edits/e1/review").json()
        self.assertEqual(d["which"], "preview")
        self.assertEqual([v["which"] for v in d["versions"]], ["v1", "final", "preview"])
        self.assertNotIn("file", d["versions"][0])                         # the page is not told where files are on disk
        self.assertEqual([c["title"] for c in d["chapters"]], ["Intro", "Headshots"])
        self.assertEqual(d["clips"][1]["captions"], "hello there")         # an `off` cue is not shown
        self.assertEqual(self.client.get("/api/edits/e1/review?which=v1").json()["clips"], [])      # an archive with no timeline
        self.assertEqual(self.client.get("/api/edits/e1/review?which=zzz").status_code, 400)

    # -- comments --------------------------------------------------------------

    def test_a_comment_is_located_in_the_edit_and_stored(self):
        r = self.post({"t": 37.5, "text": "zoom less", "kind": "change", "on": "preview"})
        self.assertEqual(r.status_code, 200)
        c = r.json()
        self.assertEqual((c["id"], c["clip"], c["offset"], c["chapter"], c["status"]), ("n1", "b", 5.5, "Headshots", "open"))
        stored = editreview.load(self.root)
        self.assertEqual(stored["comments"][0]["text"], "zoom less")
        self.assertEqual(self.client.get("/api/edits/e1/review").json()["comments"][0]["id"], "n1")

    def test_bad_comments_are_400_with_what_to_fix(self):
        for body in ({"t": 5, "text": ""}, {"t": "soon", "text": "x"}, {"t": 5, "text": "x", "kind": "rant"}, {"t": 5, "text": "x", "on": "tomorrow"}):
            r = self.post(body)
            self.assertEqual(r.status_code, 400, body)
            self.assertTrue(r.json()["detail"])
        self.assertEqual(editreview.load(self.root)["comments"], [])

    def test_resolve_reply_edit_reopen_and_delete(self):
        self.post({"t": 5, "text": "zoom less"})
        base = "/api/edits/e1/comments/n1"
        r = self.client.patch(base, json={"status": "done", "how": "zoom 1.18 -> 1.10", "in": "v2", "reply": "done in v2"}, headers=HEADERS).json()
        self.assertEqual((r["status"], r["done"]["in"], r["done"]["how"], r["reply"]), ("done", "v2", "zoom 1.18 -> 1.10", "done in v2"))
        r = self.client.patch(base, json={"status": "open", "text": "zoom less, still", "kind": "question"}, headers=HEADERS).json()
        self.assertEqual((r["status"], r["done"], r["text"], r["kind"]), ("open", None, "zoom less, still", "question"))
        self.assertEqual(self.client.patch(base, json={"status": "maybe"}, headers=HEADERS).status_code, 400)
        self.assertEqual(self.client.delete(base, headers=HEADERS).json(), {"deleted": "n1"})
        self.assertEqual(self.client.delete(base, headers=HEADERS).status_code, 404)
        self.assertEqual(self.client.patch("/api/edits/e1/comments/n9", json={"text": "x"}, headers=HEADERS).status_code, 404)

    def test_a_taste_comment_becomes_a_rule_and_closes(self):
        self.post({"t": 5, "text": "captions bigger on phones", "kind": "taste"})
        r = self.post({"text": "captions at least 6% of the frame"}, path="/api/edits/e1/comments/n1/taste")
        self.assertEqual(r.status_code, 200)
        rule = r.json()["rule"]
        self.assertEqual((rule["id"], rule["text"], rule["said"], rule["edit"]), ("t1", "captions at least 6% of the frame", "captions bigger on phones", "e1"))
        c = editreview.load(self.root)["comments"][0]
        self.assertEqual((c["status"], c["done"]["how"]), ("done", "taste rule t1"))
        self.assertEqual(json.loads((self.work / "_edits" / "taste.json").read_text(encoding="utf-8"))["rules"][0]["text"], "captions at least 6% of the frame")
        self.assertEqual(self.client.get("/api/edits/e1/review").json()["taste"], [{"id": "t1", "text": "captions at least 6% of the frame"}])
        self.assertEqual(self.post({}, path="/api/edits/e1/comments/n9/taste").status_code, 404)

    def test_mutating_requests_need_the_clipbot_header(self):
        self.assertEqual(self.client.post("/api/edits/e1/comments", json={"t": 5, "text": "x"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/edits/e1/comments/n1").status_code, 403)

    # -- names and media ---------------------------------------------------------

    def test_edit_names_are_names_never_paths(self):
        for bad in (".hidden", "a b", "x" * 200, "%2E%2E"):
            self.assertEqual(self.client.get("/api/edits/{0}/review".format(bad)).status_code, 400, bad)      # the name check itself
        self.assertEqual(self.client.get("/api/edits/..%5C..%5Cx/review").status_code, 400)
        self.assertEqual(self.client.get("/api/edits/a%2Fb/review").status_code, 404)             # a slash is a different route, not an edit

    def test_a_render_is_served_with_range_requests(self):
        r = self.client.get("/media/edit/e1/preview", headers={"Range": "bytes=10-19"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.content, VIDEO[10:20])
        self.assertEqual(self.client.get("/media/edit/e1/final").content, VIDEO + b"x")
        self.assertEqual(self.client.get("/media/edit/e1/v1").content, VIDEO + b"yy")

    def test_unknown_renders_and_bad_names_are_refused(self):
        self.assertEqual(self.client.get("/media/edit/e1/v9").status_code, 404)
        self.assertEqual(self.client.get("/media/edit/e1/%2E%2E").status_code, 400)
        self.assertEqual(self.client.get("/media/edit/e1/preview.mp4").status_code, 400)
        self.assertEqual(self.client.get("/media/edit/nope/preview").status_code, 404)


if __name__ == "__main__":
    unittest.main()
