"""OAuth for uploading to YouTube (the "installed app" loopback flow, with PKCE).

No Google client library is used on purpose: the whole flow is two form POSTs
and one loopback redirect, and `ytupload.py` needs raw control of the resumable
protocol anyway. That keeps `requirements.txt` unchanged.

Credentials come from a Google Cloud project the *user* creates (ClipBot ships
none and cannot): a "Desktop app" OAuth client whose JSON is saved at
`upload.youtube.client_secret_file`. `python -m clipbot youtube-auth` then walks
the one-time consent in a browser and stores a refresh token under
`work/_cache/` (gitignored with the rest of `work/`).

Only the `youtube.upload` scope is requested - ClipBot can add videos to the
channel but cannot read it, edit other videos, or delete anything.
"""

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .config import PROJECT_ROOT, Settings
from .utils import StageError, get_logger

log = get_logger(__name__)

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_CLIENT_FILE = "config/youtube_client_secret.json"

# Refresh this long before the access token actually expires, so a chunk that
# takes a while to send never goes out with a token that dies mid-request.
EXPIRY_MARGIN_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 30

SETUP_HINT = (
    "One-time setup (about 5 minutes) - see README.md, 'Uploading to YouTube':\n"
    "  1. console.cloud.google.com: create a project, enable 'YouTube Data API v3'.\n"
    "  2. OAuth consent screen: External, add yourself as a test user, then set\n"
    "     Publishing status to 'In production' (while it is 'Testing', Google\n"
    "     expires the login every 7 days).\n"
    "  3. Credentials: create an OAuth client ID of type 'Desktop app' and download\n"
    "     its JSON to {client_file}\n"
    "  4. Run: python -m clipbot youtube-auth"
)


# --- paths -----------------------------------------------------------------


def client_secret_path(settings: Settings) -> Path:
    value = settings.get("upload.youtube.client_secret_file") or DEFAULT_CLIENT_FILE
    path = Path(str(value))
    return path if path.is_absolute() else PROJECT_ROOT / path


def token_path(settings: Settings) -> Path:
    value = settings.get("upload.youtube.token_file")
    if value:
        path = Path(str(value))
        return path if path.is_absolute() else PROJECT_ROOT / path
    # `_cache` is shared infrastructure under work/ (gitignored), so a refresh
    # token can't be committed by accident.
    return settings.work_root / "_cache" / "youtube_token.json"


# --- client credentials ----------------------------------------------------


def load_client(settings: Settings) -> Dict[str, str]:
    """Read the downloaded OAuth client JSON. Raises StageError with the setup
    steps when it is missing or is the wrong kind of client."""
    path = client_secret_path(settings)
    if not path.is_file():
        raise StageError(
            "No YouTube OAuth client found at {0}.\n\n{1}".format(
                path, SETUP_HINT.format(client_file=path)
            )
        )
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        raise StageError("Could not read {0}: {1}".format(path, exc))

    block = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        raise StageError(
            "{0} is not a 'Desktop app' OAuth client (expected an \"installed\" "
            "section). In Google Cloud Console create the client with application "
            "type 'Desktop app' and download that JSON.".format(path)
        )
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    if not client_id or not client_secret:
        raise StageError("{0} has no client_id / client_secret.".format(path))
    return {
        "client_id": str(client_id),
        "client_secret": str(client_secret),
        "auth_uri": str(block.get("auth_uri") or DEFAULT_AUTH_URI),
        "token_uri": str(block.get("token_uri") or DEFAULT_TOKEN_URI),
    }


# --- HTTP ------------------------------------------------------------------


def _post_form(url: str, fields: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """POST form fields, return (status, parsed JSON). Never raises on a 4xx -
    Google reports OAuth errors as 400 with a JSON body that callers inspect."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise StageError("Could not reach Google's OAuth server: {0}".format(exc))
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        parsed = {"error": "unparseable_response", "error_description": body[:200].decode("utf-8", "replace")}
    return status, parsed if isinstance(parsed, dict) else {}


# --- PKCE + consent --------------------------------------------------------


def pkce_pair() -> Tuple[str, str]:
    """(verifier, S256 challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, within the 43-128 range
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


_DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>ClipBot</title>"
    "<body style='font-family:system-ui;margin:3rem'>"
    "<h2>{0}</h2><p>You can close this tab and go back to ClipBot.</p></body>"
)


def authorize(
    settings: Settings,
    open_browser: bool = True,
    timeout: float = 300.0,
    echo: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Run the one-time consent and store the refresh token. Returns the saved
    token document. Blocks until the browser redirect arrives or `timeout`."""
    client = load_client(settings)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    box = {}  # type: Dict[str, Any]

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            query = urllib.parse.urlparse(self.path).query
            params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
            # Browsers also ask for /favicon.ico; only a real redirect counts.
            if "code" not in params and "error" not in params:
                self.send_response(404)
                self.end_headers()
                return
            box["params"] = params
            ok = "code" in params
            body = _DONE_PAGE.format(
                "Authorised - ClipBot can now upload." if ok else "Authorisation was declined."
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the default stderr access log
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.timeout = 1.0
    redirect_uri = "http://127.0.0.1:{0}".format(server.server_address[1])
    auth_url = client["auth_uri"] + "?" + urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # offline + consent is what makes Google hand back a refresh token
            # every time, not only on the very first authorisation.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    echo("Opening your browser to authorise ClipBot to upload to YouTube.")
    echo("If it does not open, paste this address into a browser:\n\n  " + auth_url + "\n")
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:  # headless box: the printed URL is the fallback
            pass

    deadline = time.time() + timeout
    try:
        while "params" not in box and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    params = box.get("params")
    if not params:
        raise StageError("Timed out waiting for the browser authorisation ({0:.0f}s).".format(timeout))
    if params.get("error"):
        raise StageError("Authorisation failed: {0}".format(params.get("error")))
    if params.get("state") != state:
        raise StageError("Authorisation response had the wrong 'state' - aborting for safety.")

    status, tok = _post_form(
        client["token_uri"],
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": params["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if status != 200 or "access_token" not in tok:
        raise StageError(
            "Google refused the authorisation code: {0} {1}".format(
                tok.get("error", status), tok.get("error_description", "")
            ).strip()
        )
    if not tok.get("refresh_token"):
        raise StageError(
            "Google did not return a refresh token. Remove ClipBot at "
            "myaccount.google.com/permissions and run youtube-auth again."
        )
    if SCOPE not in str(tok.get("scope", SCOPE)):
        raise StageError("Google granted a different scope than requested: {0}".format(tok.get("scope")))

    document = {
        "refresh_token": tok["refresh_token"],
        "access_token": tok["access_token"],
        "expires_at": time.time() + float(tok.get("expires_in", 3600)),
        "scope": tok.get("scope", SCOPE),
        "client_id": client["client_id"],
        "saved_at": time.time(),
    }
    _save_token(token_path(settings), document)
    return document


def _save_token(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2)
    tmp.replace(path)


# --- using the token -------------------------------------------------------


class TokenSource(object):
    """Hands out a valid access token, refreshing it as needed.

    Callable: `source()` returns the bearer token, `source(force_refresh=True)`
    discards the cached one first (used after a 401). Thread-safe, because a
    long upload keeps asking for a token for hours.
    """

    def __init__(self, settings: Settings, now: Callable[[], float] = time.time):
        self._client = load_client(settings)
        self._path = token_path(settings)
        self._now = now
        self._lock = threading.Lock()
        self._doc = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self._path.is_file():
            raise StageError(
                "ClipBot is not authorised to upload yet. Run:\n"
                "  python -m clipbot youtube-auth"
            )
        try:
            with self._path.open("r", encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (ValueError, OSError) as exc:
            raise StageError("Could not read {0}: {1}".format(self._path, exc))
        if not isinstance(doc, dict) or not doc.get("refresh_token"):
            raise StageError("{0} has no refresh token - run youtube-auth again.".format(self._path))
        if doc.get("client_id") != self._client["client_id"]:
            raise StageError(
                "The saved login belongs to a different OAuth client. "
                "Run `python -m clipbot youtube-auth` again."
            )
        return doc

    def _refresh(self) -> None:
        status, tok = _post_form(
            self._client["token_uri"],
            {
                "client_id": self._client["client_id"],
                "client_secret": self._client["client_secret"],
                "refresh_token": self._doc["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if status != 200 or "access_token" not in tok:
            if tok.get("error") == "invalid_grant":
                raise StageError(
                    "Google no longer accepts the saved login (it was revoked or "
                    "expired). Run `python -m clipbot youtube-auth` again.\n"
                    "If this keeps happening about weekly, the OAuth consent screen "
                    "is still in 'Testing' - publish it ('In production') to stop the "
                    "7-day expiry."
                )
            raise StageError(
                "Could not refresh the YouTube login: {0} {1}".format(
                    tok.get("error", status), tok.get("error_description", "")
                ).strip()
            )
        self._doc["access_token"] = tok["access_token"]
        self._doc["expires_at"] = self._now() + float(tok.get("expires_in", 3600))
        if tok.get("refresh_token"):  # Google rarely rotates it, but may
            self._doc["refresh_token"] = tok["refresh_token"]
        _save_token(self._path, self._doc)

    def __call__(self, force_refresh: bool = False) -> str:
        with self._lock:
            stale = (
                force_refresh
                or not self._doc.get("access_token")
                or float(self._doc.get("expires_at", 0)) - self._now() < EXPIRY_MARGIN_SECONDS
            )
            if stale:
                self._refresh()
            return str(self._doc["access_token"])


# --- status (offline) ------------------------------------------------------


def status(settings: Settings) -> Dict[str, Any]:
    """What the Doctor and the dashboard need to know, without any network.

    `state` is one of `not_configured` (no client file - the feature simply
    isn't set up), `needs_auth` (client present, never authorised) or `ready`.
    """
    client_file = client_secret_path(settings)
    try:
        client = load_client(settings)
    except StageError as exc:
        missing = not client_file.is_file()
        return {
            "state": "not_configured",
            "authorized": False,
            "detail": (
                "not set up (optional) - needed only for Upload to YouTube. "
                "Put a Desktop-app OAuth client JSON at {0}".format(client_file)
                if missing
                else str(exc).splitlines()[0]
            ),
        }
    path = token_path(settings)
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not doc.get("refresh_token") or doc.get("client_id") != client["client_id"]:
        return {
            "state": "needs_auth",
            "authorized": False,
            "detail": "client found, not authorised yet - run: python -m clipbot youtube-auth",
        }
    return {"state": "ready", "authorized": True, "detail": "authorised (upload scope only)"}
