"""Dashboard side of the YouTube upload feature: the job lane, the stage row,
the Doctor row and the "To YouTube" create-workspace path.

Uses FastAPI's test client without its context manager, so the app's startup
hook (which would spin up the real job threads and log bridge) never runs.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from clipbot.server import app as app_module
    from clipbot.server import jobs as jobs_module
except Exception as exc:  # pragma: no cover - server extras not installed
    TestClient = None
    _IMPORT_ERROR = exc

from clipbot import youtube_auth
from clipbot.config import Settings
from clipbot.workspace import Workspace

KICK_URL = "https://kick.com/gitanshteck/videos/e0ec2d2c-bad4-49b9-8d84-e2aa80ff6112"
YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
HEADERS = {"X-ClipBot": "1"}


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestUploadDashboard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.work.mkdir()
        self.settings = Settings(
            {
                "work_root": str(self.work),
                "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "yt_dlp": "yt-dlp"},
                "upload": {"youtube": {"privacy": "private"}},
            },
            Path("settings.json"),
        )
        patcher = mock.patch.object(app_module, "settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")

    # -- lane ---------------------------------------------------------------

    def test_upload_runs_on_its_own_lane(self):
        runner = jobs_module.JobRunner(self.settings, jobs_module.EventBus())
        runner.register("upload", lambda *a: None)
        runner.register("chat", lambda *a: None)
        runner.register("download", lambda *a: None)
        runner.submit("upload", "ws-a", {})
        runner.submit("chat", "ws-b", {})
        runner.submit("download", "ws-c", {})
        self.assertEqual(runner._queues["net"].qsize(), 1)
        self.assertEqual(runner._queues["light"].qsize(), 1)
        self.assertEqual(runner._queues["heavy"].qsize(), 1)

    def test_the_real_runner_has_an_upload_handler(self):
        self.assertIn("upload", app_module.runner._handlers)

    # -- stage row ----------------------------------------------------------

    def test_stage_row_is_ready_with_only_a_url_and_done_after_upload(self):
        ws = Workspace.for_url(self.work, KICK_URL)
        rows = {r["key"]: r for r in app_module.stage_states(ws)}
        self.assertIn("upload", rows)
        self.assertTrue(rows["upload"]["ready"])         # no video needed
        self.assertFalse(rows["upload"]["done"])
        self.assertFalse(rows["download"]["done"])       # ...and nothing downloaded
        order = [r["key"] for r in app_module.stage_states(ws)]
        self.assertLess(order.index("upload"), order.index("cleanup"))

        ws.mark_stage("upload", video_id="abc", url="https://youtu.be/abc")
        rows = {r["key"]: r for r in app_module.stage_states(ws)}
        self.assertTrue(rows["upload"]["done"])
        self.assertEqual(rows["upload"]["detail"]["url"], "https://youtu.be/abc")

    def test_youtube_workspaces_have_no_upload_row(self):
        ws = Workspace.for_url(self.work, YT_URL)
        self.assertNotIn("upload", [r["key"] for r in app_module.stage_states(ws)])

    # -- doctor -------------------------------------------------------------

    def test_doctor_reports_upload_state_without_failing_when_unconfigured(self):
        with mock.patch.object(app_module.youtube_stage, "health_checks", lambda s: []), \
                mock.patch.object(app_module.ytsegments, "ffmpeg_supports_request_size", lambda s: None):
            body = self.client.get("/api/doctor").json()
        row = next(c for c in body["checks"] if c["name"] == "YouTube upload (OAuth)")
        self.assertTrue(row["ok"])  # optional feature: not set up is not a failure
        self.assertIn("optional", row["detail"])

    def test_doctor_flags_a_client_that_was_never_authorised(self):
        needs = {"state": "needs_auth", "authorized": False, "detail": "run youtube-auth"}
        with mock.patch.object(youtube_auth, "status", lambda s: needs), \
                mock.patch.object(app_module.youtube_stage, "health_checks", lambda s: []), \
                mock.patch.object(app_module.ytsegments, "ffmpeg_supports_request_size", lambda s: None):
            body = self.client.get("/api/doctor").json()
        row = next(c for c in body["checks"] if c["name"] == "YouTube upload (OAuth)")
        self.assertFalse(row["ok"])

    # -- "To YouTube" -------------------------------------------------------

    def post(self, **payload):
        return self.client.post("/api/workspaces", json=payload, headers=HEADERS)

    def test_upload_action_is_refused_before_creating_anything_when_not_authorised(self):
        not_ready = {"state": "not_configured", "authorized": False, "detail": "not set up"}
        with mock.patch.object(youtube_auth, "status", lambda s: not_ready):
            res = self.post(url=KICK_URL, action="upload")
        self.assertEqual(res.status_code, 400)
        self.assertIn("not set up", res.json()["detail"])
        self.assertEqual(list(self.work.iterdir()), [])  # no empty workspace left behind

    def test_upload_action_submits_an_upload_job_not_a_pipeline(self):
        ready = {"state": "ready", "authorized": True, "detail": "ok"}
        submitted = {}

        class FakeJob(object):
            def to_dict(self):
                return {"id": "j1"}

        def fake_submit(kind, slug, options=None):
            submitted.update(kind=kind, slug=slug, options=options)
            return FakeJob()

        with mock.patch.object(youtube_auth, "status", lambda s: ready), \
                mock.patch.object(app_module.runner, "submit", fake_submit):
            res = self.post(url=KICK_URL, action="upload", privacy="unlisted", quality="720")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(submitted["kind"], "upload")
        self.assertEqual(submitted["options"]["privacy"], "unlisted")
        self.assertEqual(submitted["options"]["quality"], "720")
        self.assertEqual(submitted["options"]["url"], KICK_URL)
        # the workspace exists for the job to work in, but no video was fetched
        ws = Workspace(self.work / res.json()["slug"])
        self.assertTrue(ws.root.is_dir())
        self.assertIsNone(ws.video_path())

    def test_default_action_is_still_the_pipeline(self):
        submitted = {}

        class FakeJob(object):
            def to_dict(self):
                return {"id": "j1"}

        def fake_submit(kind, slug, options=None):
            submitted["kind"] = kind
            return FakeJob()

        with mock.patch.object(app_module.runner, "submit", fake_submit):
            res = self.post(url=KICK_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(submitted["kind"], "pipeline")

    def test_bad_privacy_and_bad_action_are_rejected(self):
        ready = {"state": "ready", "authorized": True, "detail": "ok"}
        with mock.patch.object(youtube_auth, "status", lambda s: ready):
            self.assertEqual(self.post(url=KICK_URL, action="upload", privacy="everyone").status_code, 400)
        self.assertEqual(self.post(url=KICK_URL, action="explode").status_code, 400)

    def test_a_youtube_url_cannot_be_uploaded(self):
        ready = {"state": "ready", "authorized": True, "detail": "ok"}
        with mock.patch.object(youtube_auth, "status", lambda s: ready):
            res = self.post(url=YT_URL, action="upload")
        self.assertEqual(res.status_code, 400)
        self.assertIn("already a YouTube video", res.json()["detail"])

    def test_mutating_routes_still_need_the_csrf_header(self):
        res = self.client.post("/api/workspaces", json={"url": KICK_URL, "action": "upload"})
        self.assertEqual(res.status_code, 403)

    # -- the handler --------------------------------------------------------

    def test_job_handler_passes_options_through_and_returns_the_url(self):
        ws = Workspace.for_url(self.work, KICK_URL)
        job = mock.Mock(options={"privacy": "unlisted", "force": True, "quality": "720", "title": "T"})
        seen = {}

        def fake_upload(ws_, st, **kw):
            seen.update(kw)
            return {"url": "https://youtu.be/abc"}

        with mock.patch.object(app_module.upload_stage, "upload_vod", fake_upload):
            result = app_module._h_upload(ws, self.settings, job, mock.Mock())
        self.assertEqual(result, "https://youtu.be/abc")
        self.assertEqual(seen["privacy"], "unlisted")
        self.assertTrue(seen["force"])
        self.assertEqual(seen["title"], "T")
        self.assertIsNone(seen["mode"])  # unset -> the stage reads the setting


if __name__ == "__main__":
    unittest.main()
