"""YouTube's resumable upload protocol, spoken directly over HTTP.

Why not Google's client library: its resumable upload wants a seekable stream
(it re-reads on retry), which is exactly what a VOD relayed straight from Kick
is not, and it hides the chunk buffering, retry and cancellation behaviour this
module needs to control. The protocol itself is small:

    POST  .../upload/youtube/v3/videos?uploadType=resumable   -> Location: <session>
    PUT   <session>   Content-Range: bytes START-END/TOTAL    -> 308 (more) | 200 (done)
    PUT   <session>   Content-Range: bytes */TOTAL            -> "how much do you have?"

Two ways to feed it, both through `ResumableUpload`:

* `upload_file`   - the size is known. Chunks are read from disk by offset, so a
  failed chunk is simply re-read, and the session URI can be kept across
  process restarts to resume a half-sent file.
* `upload_stream` - the size is NOT known (a VOD being remuxed on the fly). Each
  non-final chunk is sent with `Content-Range: bytes S-E/*` and only the last
  one states the total. Memory is bounded to a couple of chunks; a chunk is held
  until the server acknowledges it, which is what makes retries possible
  without ever rewinding the source. Google documents unknown-length uploads for
  its upload protocol in general but YouTube's own guide shows only known
  sizes, so a refusal surfaces as `UnknownLengthUnsupported` carrying the
  unsent bytes, and the caller falls back to spooling to disk.

Nothing here knows about Kick, ffmpeg or workspaces.
"""

import http.client
import json
import queue
import random
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from .progress import NULL_PROGRESS, Progress
from .utils import StageError, get_logger

log = get_logger(__name__)

UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"

# The protocol requires every chunk but the last to be a multiple of 256 KiB.
CHUNK_UNIT = 256 * 1024
DEFAULT_CHUNK_BYTES = 16 * 1024 * 1024

PRIVACY_CHOICES = ("private", "unlisted", "public")
TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000

# HTTP statuses a server uses to say "I will not take a body of unknown length".
_LENGTH_REJECTED = (411, 413, 416)
_TRANSIENT_STATUS = (408, 429, 500, 502, 503, 504)
_TRANSIENT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "backendError")


class UploadError(StageError):
    """An upload failed in a way worth showing to the user."""


class SessionExpired(UploadError):
    """The resumable session URI is gone (404/410): start a new upload."""


class UnknownLengthUnsupported(UploadError):
    """The server won't take an upload without a declared size.

    `pending` holds the chunks that were read from the source but not yet
    acknowledged, so the caller can spool them and carry on with a known size.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.pending = []  # type: List[bytes]


class _Retry(Exception):
    """Internal: a transient failure worth another attempt."""


# --- metadata --------------------------------------------------------------


def clean_title(title: Optional[str], fallback: str = "Untitled stream") -> str:
    """YouTube rejects titles over 100 characters or containing < or >."""
    text = " ".join(str(title or "").replace("<", "‹").replace(">", "›").split())
    if not text:
        text = fallback
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def clean_description(text: Optional[str]) -> str:
    """No < or >, and at most 5000 *bytes* of UTF-8 (cut on a character edge)."""
    value = str(text or "").replace("<", "‹").replace(">", "›").strip()
    raw = value.encode("utf-8")
    if len(raw) <= DESCRIPTION_MAX_BYTES:
        return value
    return raw[:DESCRIPTION_MAX_BYTES].decode("utf-8", "ignore").rstrip()


def build_metadata(
    title: str,
    description: str = "",
    privacy: str = "private",
    category_id: str = "20",
    made_for_kids: bool = False,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if privacy not in PRIVACY_CHOICES:
        raise UploadError(
            "Unknown privacy '{0}' - choose one of: {1}".format(privacy, ", ".join(PRIVACY_CHOICES))
        )
    snippet = {
        "title": clean_title(title),
        "description": clean_description(description),
        "categoryId": str(category_id),
    }  # type: Dict[str, Any]
    if tags:
        snippet["tags"] = [str(t) for t in tags][:30]
    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }


# --- errors ----------------------------------------------------------------

_HINTS = {
    "quotaExceeded": "The Google Cloud project's YouTube API quota is used up for today; it resets at midnight Pacific.",
    "uploadLimitExceeded": "This channel has reached YouTube's upload limit for now. Try again later.",
    "youtubeSignupRequired": "The Google account has no YouTube channel yet - create one at youtube.com, then retry.",
    "forbidden": "YouTube refused this account/app. Re-run `python -m clipbot youtube-auth`.",
    "insufficientPermissions": "The saved login lacks upload permission. Re-run `python -m clipbot youtube-auth`.",
    "authError": "The saved login was rejected. Re-run `python -m clipbot youtube-auth`.",
    "invalidTitle": "YouTube rejected the title.",
    "invalidDescription": "YouTube rejected the description.",
    "invalidCategoryId": "upload.youtube.category_id is not a valid YouTube category for this channel's region.",
    "videoTooLong": "YouTube rejected the video as too long. Unverified channels are limited to 15 minutes - verify the channel by phone in YouTube Studio to lift that.",
}


def _parse_error(body: bytes) -> Tuple[str, str]:
    try:
        doc = json.loads(body.decode("utf-8") or "{}")
        err = doc.get("error") or {}
        if isinstance(err, str):
            return "", err
        first = (err.get("errors") or [{}])[0]
        return str(first.get("reason") or ""), str(err.get("message") or "")
    except (ValueError, AttributeError, IndexError):
        return "", body[:200].decode("utf-8", "replace")


def explain_api_error(status: int, body: bytes) -> str:
    reason, message = _parse_error(body)
    text = "YouTube refused the upload (HTTP {0}{1}){2}".format(
        status, ", " + reason if reason else "", ": " + message if message else ""
    )
    if reason in _HINTS:
        text += "\n" + _HINTS[reason]
    return text


# --- transport -------------------------------------------------------------


def _http(method: str, url: str, headers: Dict[str, str], body: Any, timeout: float):
    """One request on a fresh connection. http.client rather than urllib: a
    resumable upload's 308 ("Resume Incomplete") is a status urllib wants to
    treat as a redirect."""
    parts = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.netloc, timeout=timeout)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def _read_exactly(source, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        piece = source.read(n - len(buf))
        if not piece:
            break
        buf += piece
    return bytes(buf)


class ChunkReader(object):
    """Reads `source` in fixed-size chunks on a background thread.

    One chunk is read ahead while the previous one is on the wire, which is what
    lets the download from Kick and the upload to YouTube overlap. Memory is
    bounded to roughly three chunks (one sending, one queued, one being read).
    `next_chunk()` returns b"" at end of input, repeatedly.
    """

    def __init__(self, source, chunk_bytes: int):
        self._source = source
        self._chunk = int(chunk_bytes)
        self._q = queue.Queue(maxsize=1)  # type: queue.Queue
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self._eof = False

    def _put(self, item) -> bool:
        while not self._stop.is_set():
            try:
                self._q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                data = _read_exactly(self._source, self._chunk)
                if not self._put(data):
                    return
                if not data:
                    return
        except BaseException as exc:  # surfaced to the consumer, never lost
            self._put(exc)

    def next_chunk(self) -> bytes:
        if self._eof:
            return b""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="clipbot-readahead")
            self._thread.daemon = True
            self._thread.start()
        item = self._q.get()
        if isinstance(item, BaseException):
            self._eof = True
            raise item
        if not item:
            self._eof = True
        return item

    def close(self) -> None:
        self._stop.set()


# --- the uploader ----------------------------------------------------------


class ResumableUpload(object):
    def __init__(
        self,
        token_source: Callable[..., str],
        progress: Progress = NULL_PROGRESS,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        max_retries: int = 8,
        endpoint: Optional[str] = None,
        sleep: Optional[Callable[[float], None]] = None,
        timeout: float = 120.0,
    ):
        self.token = token_source
        self.progress = progress
        self.chunk_bytes = max(CHUNK_UNIT, (int(chunk_bytes) // CHUNK_UNIT) * CHUNK_UNIT)
        self.max_retries = int(max_retries)
        # Resolved here, not in the signature, so tests (and a future setting)
        # can point UPLOAD_ENDPOINT / time.sleep somewhere else.
        self.endpoint = endpoint or UPLOAD_ENDPOINT
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self.timeout = float(timeout)
        # Sent as the Content-Type of every chunk. start_session() records the
        # type it declared; upload_file/upload_stream take it explicitly too,
        # for a session resumed by a later process that never called it.
        self._mime = "application/octet-stream"

    # ---- plumbing ---------------------------------------------------------

    def _pause(self, attempt: int, why: str) -> None:
        delay = min(60.0, 2.0 ** attempt) + random.uniform(0.0, 1.0)
        log.warning("Upload hiccup (%s) - retry %d/%d in %.0fs", why, attempt, self.max_retries, delay)
        remaining = delay
        while remaining > 0:
            self.progress.check_cancelled()
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step

    def _do(self, method: str, url: str, headers: Dict[str, str], body: Any = None):
        """One authenticated request. Refreshes the token once on a 401 and turns
        network errors and transient statuses into `_Retry`."""
        refreshed = False
        while True:
            hdrs = dict(headers)
            hdrs["Authorization"] = "Bearer " + self.token(force_refresh=refreshed)
            try:
                status, rh, data = _http(method, url, hdrs, body, self.timeout)
            except (OSError, http.client.HTTPException) as exc:
                raise _Retry("network error: {0}".format(exc))
            if status == 401 and not refreshed:
                refreshed = True
                continue
            if status in _TRANSIENT_STATUS:
                raise _Retry("HTTP {0}".format(status))
            if status == 403 and _parse_error(data)[0] in _TRANSIENT_REASONS:
                raise _Retry("HTTP 403 rate limit")
            return status, rh, data

    def _interpret(self, status: int, rh: Dict[str, str], data: bytes):
        """Map a chunk/status response to ("complete", None, video) or
        ("incomplete", committed_bytes, None)."""
        if status in (200, 201):
            try:
                return "complete", None, json.loads(data.decode("utf-8"))
            except ValueError:
                raise UploadError("YouTube accepted the upload but returned an unreadable reply.")
        if status == 308:
            rng = rh.get("range", "")
            if rng.startswith("bytes=") and "-" in rng:
                try:
                    return "incomplete", int(rng.rsplit("-", 1)[1]) + 1, None
                except ValueError:
                    pass
            return "incomplete", 0, None
        if status in (404, 410):
            raise SessionExpired(
                "The upload session expired or was discarded by YouTube (HTTP {0}).".format(status)
            )
        raise UploadError(explain_api_error(status, data))

    # ---- protocol ---------------------------------------------------------

    def start_session(self, metadata: Dict[str, Any], size: Optional[int] = None, mime: str = "video/*") -> str:
        self._mime = mime
        body = json.dumps(metadata).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Content-Length": str(len(body)),
            "X-Upload-Content-Type": mime,
        }
        if size is not None:
            headers["X-Upload-Content-Length"] = str(int(size))
        url = self.endpoint + "?uploadType=resumable&part=snippet,status"
        attempt = 0
        while True:
            try:
                status, rh, data = self._do("POST", url, headers, body)
                break
            except _Retry as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise UploadError("Could not start the YouTube upload: {0}".format(exc))
                self._pause(attempt, str(exc))
        if status == 200 and rh.get("location"):
            return rh["location"]
        reason = _parse_error(data)[0]
        if size is None and (
            status in _LENGTH_REJECTED
            or (status == 400 and reason in ("", "badRequest", "invalidRequest", "mediaBodyRequired"))
        ):
            raise UnknownLengthUnsupported(
                "YouTube would not start an upload without a declared size (HTTP {0}).".format(status)
            )
        raise UploadError(explain_api_error(status, data))

    def query(self, session: str, total: Optional[int]):
        """Ask the server how much of the upload it holds."""
        headers = {
            "Content-Length": "0",
            "Content-Range": "bytes */{0}".format("*" if total is None else int(total)),
        }
        status, rh, data = self._do("PUT", session, headers, b"")
        return self._interpret(status, rh, data)

    def probe(self, session: str, total: Optional[int]):
        """`query`, but None instead of an exception when the session can't be
        used - how a saved session is judged resumable or not."""
        try:
            return self.query(session, total)
        except (SessionExpired, UploadError, _Retry) as exc:
            log.info("Saved upload session is not usable (%s)", exc)
            return None

    def _send_chunk(self, session: str, view, start: int, total: Optional[int]):
        last = start + len(view) - 1
        headers = {
            "Content-Length": str(len(view)),
            "Content-Range": "bytes {0}-{1}/{2}".format(start, last, "*" if total is None else int(total)),
            "Content-Type": self._mime,
        }
        status, rh, data = self._do("PUT", session, headers, view)
        if total is None and start == 0 and (
            status in _LENGTH_REJECTED
            or (status == 400 and _parse_error(data)[0] in ("", "badRequest", "invalidRequest"))
        ):
            raise UnknownLengthUnsupported(
                "YouTube would not accept an upload of unknown length (HTTP {0}).".format(status)
            )
        return self._interpret(status, rh, data)

    def _transmit(self, session: str, data: bytes, start: int, total: Optional[int]) -> Optional[Dict[str, Any]]:
        """Send `data` (which begins at byte `start` of the video) until the
        server has acknowledged all of it. Returns the video resource if the
        server reported the upload complete, else None.

        Handles three real-world cases: a transient failure (back off, ask the
        server what it has, carry on), a *partial* acknowledgement (resend only
        the remainder), and a stale position (refuse - rewinding a live stream
        is impossible, and silently skipping bytes would corrupt the video).
        """
        pos = start
        end = start + len(data)
        view = memoryview(data)
        attempt = 0
        while True:
            self.progress.check_cancelled()
            try:
                try:
                    kind, committed, result = self._send_chunk(session, view[pos - start:], pos, total)
                except _Retry as exc:
                    attempt += 1
                    if attempt > self.max_retries:
                        raise UploadError(
                            "Upload kept failing at byte {0:,} ({1}). The session is kept; "
                            "re-run to resume.".format(pos, exc)
                        )
                    self._pause(attempt, str(exc))
                    kind, committed, result = self.query(session, total)
            except _Retry as exc:  # the status query itself failed - go round again
                attempt += 1
                if attempt > self.max_retries:
                    raise UploadError("Could not reach YouTube to resume the upload: {0}".format(exc))
                self._pause(attempt, str(exc))
                continue
            if kind == "complete":
                return result
            if committed >= end:
                return None
            if committed < pos:
                raise UploadError(
                    "YouTube reports {0:,} bytes received but {1:,} were already sent; "
                    "cannot rewind.".format(committed, pos)
                )
            if committed > pos:
                attempt = 0  # made progress
            pos = committed

    def _finish(self, session: str, total: int) -> Dict[str, Any]:
        """All bytes are on the server; fetch the finished video resource."""
        for attempt in range(1, self.max_retries + 1):
            try:
                kind, _committed, result = self.query(session, total)
            except _Retry as exc:
                self._pause(attempt, str(exc))
                continue
            if kind == "complete":
                return result
            self._pause(attempt, "waiting for YouTube to confirm")
        raise UploadError("YouTube received every byte but never confirmed the video.")

    def _report(self, pos: int, total: Optional[int], started: float, base: int) -> None:
        elapsed = max(time.time() - started, 1e-6)
        self.progress.update(pos, total=total, rate=(pos - base) / elapsed)

    # ---- public -----------------------------------------------------------

    def upload_file(
        self, path, session: str, size: int, offset: int = 0, mime: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send `path` (exactly `size` bytes) starting at `offset`."""
        if mime:
            self._mime = mime
        if size <= 0:
            raise UploadError("The file to upload is empty.")
        pos = int(offset)
        started = time.time()
        base = pos
        with open(str(path), "rb") as fh:
            while pos < size:
                fh.seek(pos)
                data = fh.read(min(self.chunk_bytes, size - pos))
                if not data:
                    raise UploadError("The file changed size during the upload.")
                result = self._transmit(session, data, pos, size)
                pos += len(data)
                self._report(pos, size, started, base)
                if result is not None:
                    return result
        return self._finish(session, size)

    def upload_stream(
        self, reader: ChunkReader, session: str, size_hint: Optional[int] = None,
        mime: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """Send chunks from `reader` until it is exhausted. Returns the video
        resource and the number of bytes sent."""
        if mime:
            self._mime = mime
        offset = 0
        started = time.time()
        cur = reader.next_chunk()
        if not cur:
            raise UploadError("The source produced no data to upload.")
        while True:
            nxt = reader.next_chunk()  # blocks only until the next chunk is read
            final = not nxt
            total = offset + len(cur) if final else None
            try:
                result = self._transmit(session, cur, offset, total)
            except UnknownLengthUnsupported as exc:
                exc.pending = [cur] + ([nxt] if nxt else [])
                raise
            offset += len(cur)
            self._report(offset, size_hint, started, 0)
            if final:
                return (result if result is not None else self._finish(session, offset)), offset
            cur = nxt
