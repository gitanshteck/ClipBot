"""Tests for clipbot/youtube_auth.py.

Google is never contacted: the client JSON's `token_uri` points at a local HTTP
server standing in for oauth2.googleapis.com, and `webbrowser.open` is replaced
by a function that plays the part of the browser by hitting the loopback
redirect. What is checked is the real flow - PKCE, `state`, the code exchange,
token persistence, refresh - not a mock of it.
"""

import base64
import hashlib
import http.server
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from clipbot import youtube_auth
from clipbot.config import Settings
from clipbot.utils import StageError


class FakeGoogle(object):
    """A token endpoint that records each form it receives and replies with
    whatever `responses` holds next (or `default`)."""

    def __init__(self):
        self.forms = []
        self.responses = []
        self.default = (200, {"access_token": "AT-1", "refresh_token": "RT-1",
                              "expires_in": 3600, "scope": youtube_auth.SCOPE})
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
                fake.forms.append(form)
                status, doc = fake.responses.pop(0) if fake.responses else fake.default
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def token_uri(self):
        return "http://127.0.0.1:{0}/token".format(self.httpd.server_address[1])

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.google = FakeGoogle()
        self.addCleanup(self.google.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.client_file = self.dir / "client.json"
        self.token_file = self.dir / "token.json"
        self.write_client()
        self.settings = Settings(
            {
                "work_root": str(self.dir / "work"),
                "upload": {"youtube": {
                    "client_secret_file": str(self.client_file),
                    "token_file": str(self.token_file),
                }},
            },
            Path("settings.json"),
        )

    def write_client(self, **block):
        installed = {
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "shh",
            "auth_uri": "https://accounts.example/auth",
            "token_uri": self.google.token_uri,
        }
        installed.update(block)
        self.client_file.write_text(json.dumps({"installed": installed}), encoding="utf-8")

    def fake_browser(self, **overrides):
        """A `webbrowser.open` replacement that completes the consent."""
        seen = {}

        def open_(url):
            parts = urllib.parse.urlparse(url)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            seen.update(q)
            seen["url"] = url
            params = {"code": "AUTHCODE", "state": q["state"]}
            params.update(overrides)
            target = q["redirect_uri"] + "/?" + urllib.parse.urlencode(params)

            def hit():
                urllib.request.urlopen(target, timeout=10).read()

            threading.Thread(target=hit, daemon=True).start()
            return True

        return open_, seen


class TestPkce(unittest.TestCase):
    def test_challenge_is_the_s256_of_the_verifier(self):
        verifier, challenge = youtube_auth.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        self.assertEqual(challenge, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        self.assertNotIn("=", challenge)
        self.assertNotEqual(youtube_auth.pkce_pair()[0], verifier)


class TestClientFile(_Base):
    def test_loads_an_installed_client(self):
        client = youtube_auth.load_client(self.settings)
        self.assertEqual(client["client_id"], "cid.apps.googleusercontent.com")
        self.assertEqual(client["token_uri"], self.google.token_uri)

    def test_missing_file_explains_the_setup(self):
        self.client_file.unlink()
        with self.assertRaises(StageError) as ctx:
            youtube_auth.load_client(self.settings)
        self.assertIn("Desktop app", str(ctx.exception))
        self.assertIn("youtube-auth", str(ctx.exception))

    def test_a_web_client_is_rejected_with_a_useful_message(self):
        self.client_file.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
        with self.assertRaises(StageError) as ctx:
            youtube_auth.load_client(self.settings)
        self.assertIn("Desktop app", str(ctx.exception))


class TestAuthorize(_Base):
    def run_authorize(self, **overrides):
        open_, seen = self.fake_browser(**overrides)
        with mock.patch.object(youtube_auth.webbrowser, "open", open_):
            doc = youtube_auth.authorize(self.settings, timeout=15, echo=lambda s: None)
        return doc, seen

    def test_full_flow_saves_a_refresh_token(self):
        doc, seen = self.run_authorize()
        # what was asked of Google
        self.assertEqual(seen["scope"], youtube_auth.SCOPE)
        self.assertEqual(seen["access_type"], "offline")
        self.assertEqual(seen["prompt"], "consent")
        self.assertEqual(seen["code_challenge_method"], "S256")
        self.assertTrue(seen["redirect_uri"].startswith("http://127.0.0.1:"))
        # what was sent to the token endpoint
        form = self.google.forms[0]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "AUTHCODE")
        self.assertEqual(form["client_secret"], "shh")
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        self.assertEqual(seen["code_challenge"], base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        # what was stored
        saved = json.loads(self.token_file.read_text())
        self.assertEqual(saved["refresh_token"], "RT-1")
        self.assertEqual(saved["client_id"], "cid.apps.googleusercontent.com")
        self.assertEqual(doc["access_token"], "AT-1")

    def test_a_response_with_the_wrong_state_is_refused(self):
        with self.assertRaises(StageError) as ctx:
            self.run_authorize(state="forged")
        self.assertIn("state", str(ctx.exception))
        self.assertFalse(self.token_file.exists())
        self.assertEqual(self.google.forms, [])  # the code was never exchanged

    def test_declining_consent_is_reported(self):
        open_, _ = self.fake_browser()

        def decline(url):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
            target = q["redirect_uri"] + "/?error=access_denied&state=" + q["state"]
            threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(),
                             daemon=True).start()

        with mock.patch.object(youtube_auth.webbrowser, "open", decline):
            with self.assertRaises(StageError) as ctx:
                youtube_auth.authorize(self.settings, timeout=15, echo=lambda s: None)
        self.assertIn("access_denied", str(ctx.exception))

    def test_no_refresh_token_in_the_reply_is_an_actionable_error(self):
        self.google.default = (200, {"access_token": "AT", "expires_in": 3600})
        with self.assertRaises(StageError) as ctx:
            self.run_authorize()
        self.assertIn("refresh token", str(ctx.exception))

    def test_google_rejecting_the_code_is_reported(self):
        self.google.default = (400, {"error": "invalid_grant", "error_description": "Bad code"})
        with self.assertRaises(StageError) as ctx:
            self.run_authorize()
        self.assertIn("invalid_grant", str(ctx.exception))

    def test_times_out_when_no_browser_responds(self):
        with mock.patch.object(youtube_auth.webbrowser, "open", lambda url: True):
            with self.assertRaises(StageError) as ctx:
                youtube_auth.authorize(self.settings, timeout=1.5, echo=lambda s: None)
        self.assertIn("Timed out", str(ctx.exception))


class TestTokenSource(_Base):
    def save_token(self, expires_at, **extra):
        doc = {"refresh_token": "RT-1", "access_token": "AT-old", "expires_at": expires_at,
               "client_id": "cid.apps.googleusercontent.com"}
        doc.update(extra)
        self.token_file.write_text(json.dumps(doc))

    def test_unexpired_token_is_returned_without_a_network_call(self):
        self.save_token(expires_at=10_000)
        src = youtube_auth.TokenSource(self.settings, now=lambda: 1_000)
        self.assertEqual(src(), "AT-old")
        self.assertEqual(self.google.forms, [])

    def test_token_near_expiry_is_refreshed_and_persisted(self):
        self.save_token(expires_at=1_050)  # inside the 120 s margin at now=1000
        self.google.default = (200, {"access_token": "AT-new", "expires_in": 3600})
        src = youtube_auth.TokenSource(self.settings, now=lambda: 1_000)
        self.assertEqual(src(), "AT-new")
        self.assertEqual(self.google.forms[0]["grant_type"], "refresh_token")
        self.assertEqual(self.google.forms[0]["refresh_token"], "RT-1")
        saved = json.loads(self.token_file.read_text())
        self.assertEqual(saved["access_token"], "AT-new")
        self.assertEqual(saved["refresh_token"], "RT-1")  # kept

    def test_force_refresh_ignores_a_fresh_token(self):
        self.save_token(expires_at=10_000)
        self.google.default = (200, {"access_token": "AT-forced", "expires_in": 3600})
        src = youtube_auth.TokenSource(self.settings, now=lambda: 1_000)
        self.assertEqual(src(force_refresh=True), "AT-forced")

    def test_a_rotated_refresh_token_is_stored(self):
        self.save_token(expires_at=0)
        self.google.default = (200, {"access_token": "A", "refresh_token": "RT-2", "expires_in": 60})
        youtube_auth.TokenSource(self.settings, now=lambda: 1_000)()
        self.assertEqual(json.loads(self.token_file.read_text())["refresh_token"], "RT-2")

    def test_revoked_login_points_at_youtube_auth_and_the_testing_mode_cause(self):
        self.save_token(expires_at=0)
        self.google.default = (400, {"error": "invalid_grant"})
        with self.assertRaises(StageError) as ctx:
            youtube_auth.TokenSource(self.settings, now=lambda: 1_000)()
        text = str(ctx.exception)
        self.assertIn("youtube-auth", text)
        self.assertIn("Testing", text)

    def test_never_authorised(self):
        with self.assertRaises(StageError) as ctx:
            youtube_auth.TokenSource(self.settings)
        self.assertIn("youtube-auth", str(ctx.exception))

    def test_a_login_from_another_client_is_not_reused(self):
        self.save_token(expires_at=10_000, client_id="some-other-client")
        with self.assertRaises(StageError):
            youtube_auth.TokenSource(self.settings)


class TestStatus(_Base):
    def test_not_configured_is_not_an_error_state(self):
        self.client_file.unlink()
        st = youtube_auth.status(self.settings)
        self.assertEqual(st["state"], "not_configured")
        self.assertFalse(st["authorized"])
        self.assertIn("optional", st["detail"])

    def test_client_without_login(self):
        self.assertEqual(youtube_auth.status(self.settings)["state"], "needs_auth")

    def test_ready(self):
        self.token_file.write_text(json.dumps(
            {"refresh_token": "RT", "client_id": "cid.apps.googleusercontent.com"}))
        st = youtube_auth.status(self.settings)
        self.assertEqual(st["state"], "ready")
        self.assertTrue(st["authorized"])

    def test_status_never_touches_the_network(self):
        youtube_auth.status(self.settings)
        self.assertEqual(self.google.forms, [])


if __name__ == "__main__":
    unittest.main()
