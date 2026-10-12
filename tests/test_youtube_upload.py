"""Tests for clipbot/ytupload.py (the resumable upload client) and
clipbot/stages/upload.py (the stage built on it).

YouTube is never contacted. `FakeYouTube` is a small in-process HTTP server that
speaks the resumable-upload protocol - session creation, `Content-Range` chunks,
308 "Resume Incomplete" with a `Range` header, `bytes */N` status queries - and
can be told to misbehave in the ways real connections do: lose a chunk, store a
chunk but fail to acknowledge it, acknowledge only part of one, 401 an expired
token, expire the session, or refuse an upload of unknown length. What is tested
is therefore the real uploader code against real HTTP, not a mock of it.

ffmpeg is stubbed except in TestRealFfmpegRelay, which remuxes a real HLS
playlist served from a local HTTP server (skipped when no ffmpeg is found).
"""

import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from clipbot import youtube_auth, ytupload
from clipbot.config import PROJECT_ROOT, Settings
from clipbot.progress import JobCancelled, Progress
from clipbot.stages import upload as upload_stage
from clipbot.utils import StageError
from clipbot.workspace import Workspace

KICK_URL = "https://kick.com/gitanshteck/videos/e0ec2d2c-bad4-49b9-8d84-e2aa80ff6112"
UNIT = 256 * 1024


def payload(n, salt=0):
    """Deterministic, non-repeating-ish bytes so a misplaced chunk is caught."""
    return bytes(((i * 7) + (i >> 8) + salt) % 251 for i in range(n))


# --- the fake YouTube --------------------------------------------------------


class FakeYouTube(object):
    def __init__(self):
        self.sessions = {}
        self.requests = []           # (method, path, lowercased headers)
        self.expected_token = None   # e.g. "good": anything else gets a 401
        self.reject_unknown_at_init = False
        self.reject_unknown_at_chunk = False
        self.fail_put = {}           # data-PUT number (1-based) -> plan, see below
        self.always_503 = False
        self.expire_sessions = False
        self.on_data_put = None      # callback(put_number)
        self.data_puts = 0
        self.data_bytes_received = 0  # bytes actually carried by data PUTs
        self._next_id = 1
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, headers=None, body=b""):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, doc):
                self._send(status, {"Content-Type": "application/json"}, json.dumps(doc).encode())

            def _authorised(self):
                if fake.expected_token is None:
                    return True
                if self.headers.get("Authorization") == "Bearer " + fake.expected_token:
                    return True
                self._json(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
                return False

            def _state(self, sess):
                if sess["complete"]:
                    self._json(200, {"id": sess["video_id"], "status": {"uploadStatus": "uploaded"}})
                elif sess["data"]:
                    self._send(308, {"Range": "bytes=0-{0}".format(len(sess["data"]) - 1)})
                else:
                    self._send(308)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.requests.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}))
                if not self._authorised():
                    return
                if fake.always_503:
                    return self._send(503)
                declared = self.headers.get("X-Upload-Content-Length")
                if declared is None and fake.reject_unknown_at_init:
                    return self._json(400, {"error": {"code": 400, "message": "need a size"}})
                sid = str(fake._next_id)
                fake._next_id += 1
                fake.sessions[sid] = {
                    "data": bytearray(),
                    "metadata": json.loads(body.decode("utf-8")),
                    "declared": declared,
                    "mime": self.headers.get("X-Upload-Content-Type"),
                    "complete": False,
                    "video_id": "vid" + sid,
                    "ranges": [],
                }
                self._send(200, {"Location": fake.base + "/session/" + sid})

            def do_PUT(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                fake.requests.append(("PUT", self.path, {k.lower(): v for k, v in self.headers.items()}))
                if not self._authorised():
                    return
                sess = fake.sessions.get(self.path.rsplit("/", 1)[-1])
                if sess is None or fake.expire_sessions:
                    return self._send(404)
                cr = self.headers.get("Content-Range", "")
                if re.match(r"bytes \*/(\d+|\*)$", cr):
                    return self._state(sess)
                m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)$", cr)
                if not m:
                    return self._json(400, {"error": {"message": "bad Content-Range " + cr}})
                start, total = int(m.group(1)), m.group(3)
                fake.data_puts += 1
                put_no = fake.data_puts
                if fake.always_503:
                    return self._send(503)
                if fake.reject_unknown_at_chunk and total == "*" and start == 0:
                    return self._json(400, {"error": {"message": "unknown length"}})
                plan = fake.fail_put.get(put_no)
                if plan == "503-lost":
                    return self._send(503)
                if start != len(sess["data"]):
                    return self._state(sess)  # position mismatch: say where we are
                if isinstance(plan, tuple) and plan[0] == "partial":
                    keep = body[: plan[1]]
                    sess["data"] += keep
                    fake.data_bytes_received += len(keep)
                    return self._state(sess)
                sess["data"] += body
                fake.data_bytes_received += len(body)
                sess["ranges"].append(cr)
                if plan == "503-stored":
                    return self._send(503)
                if total != "*" and len(sess["data"]) == int(total):
                    sess["complete"] = True
                if fake.on_data_put:
                    fake.on_data_put(put_no)
                self._state(sess)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:{0}".format(self.httpd.server_address[1])

    @property
    def endpoint(self):
        return self.base + "/upload/youtube/v3/videos"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def only_session(self):
        assert len(self.sessions) == 1, "expected exactly one session, got {0}".format(len(self.sessions))
        return next(iter(self.sessions.values()))


class Tokens(object):
    """A token source: `stale` first, `good` once asked to refresh."""

    def __init__(self, first="good", after_refresh="good"):
        self.first, self.after = first, after_refresh
        self.calls = []

    def __call__(self, force_refresh=False):
        self.calls.append(force_refresh)
        return self.after if force_refresh else self.first


class _Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeYouTube()
        self.addCleanup(self.fake.close)
        self.tokens = Tokens()

    def uploader(self, **kw):
        kw.setdefault("chunk_bytes", UNIT)
        kw.setdefault("max_retries", 3)
        kw.setdefault("sleep", lambda s: None)
        return ytupload.ResumableUpload(self.tokens, endpoint=self.fake.endpoint, **kw)


META = ytupload.build_metadata("Title", "Desc")


# --- metadata ----------------------------------------------------------------


class TestMetadata(unittest.TestCase):
    def test_title_is_cleaned_and_truncated(self):
        self.assertEqual(ytupload.clean_title("a <b> c"), "a ‹b› c")
        long = ytupload.clean_title("x" * 300)
        self.assertEqual(len(long), 100)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(ytupload.clean_title("   \n "), "Untitled stream")
        self.assertEqual(ytupload.clean_title("two\nlines"), "two lines")

    def test_description_is_cut_on_a_character_edge_within_5000_bytes(self):
        text = "न" * 3000  # 3 bytes per character in UTF-8
        out = ytupload.clean_description(text)
        self.assertLessEqual(len(out.encode("utf-8")), 5000)
        out.encode("utf-8").decode("utf-8")  # no half character left behind
        self.assertEqual(ytupload.clean_description("a<b>"), "a‹b›")

    def test_build_metadata_shape_and_privacy_validation(self):
        meta = ytupload.build_metadata("T", "D", privacy="unlisted", category_id="22", made_for_kids=False)
        self.assertEqual(meta["snippet"]["categoryId"], "22")
        self.assertEqual(meta["status"]["privacyStatus"], "unlisted")
        self.assertIs(meta["status"]["selfDeclaredMadeForKids"], False)
        with self.assertRaises(StageError):
            ytupload.build_metadata("T", privacy="everyone")

    def test_api_error_explanations(self):
        body = json.dumps({"error": {"message": "x", "errors": [{"reason": "quotaExceeded"}]}}).encode()
        text = ytupload.explain_api_error(403, body)
        self.assertIn("quotaExceeded", text)
        self.assertIn("midnight Pacific", text)
        self.assertIn("HTTP 500", ytupload.explain_api_error(500, b"not json"))


# --- uploading a file of known size -------------------------------------------


class TestUploadFile(_Base):
    def run_upload(self, data, **kw):
        path = Path(tempfile.mkdtemp()) / "v.mp4"
        self.addCleanup(shutil.rmtree, str(path.parent), True)
        path.write_bytes(data)
        up = self.uploader(**kw)
        session = up.start_session(META, size=len(data), mime="video/mp4")
        return up.upload_file(path, session, len(data)), path

    def test_round_trip_is_byte_exact_and_declares_size(self):
        data = payload(UNIT * 4 + 12345)
        result, _ = self.run_upload(data)
        sess = self.fake.only_session()
        self.assertEqual(bytes(sess["data"]), data)
        self.assertEqual(result["id"], "vid1")
        self.assertEqual(sess["declared"], str(len(data)))
        self.assertEqual(sess["mime"], "video/mp4")
        self.assertEqual(sess["metadata"], META)
        # every chunk but the last is a multiple of 256 KiB, and totals are stated
        self.assertTrue(all(r.endswith("/" + str(len(data))) for r in sess["ranges"]))
        self.assertEqual(len(sess["ranges"]), 5)

    def test_chunks_carry_the_declared_mime_type(self):
        self.run_upload(payload(UNIT * 2))
        puts = [h for m, _p, h in self.fake.requests if m == "PUT"]
        self.assertTrue(puts)
        self.assertTrue(all(h.get("content-type") == "video/mp4" for h in puts))

    def test_transient_failure_is_retried(self):
        self.fake.fail_put = {2: "503-lost"}
        data = payload(UNIT * 3)
        self.run_upload(data)
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)

    def test_chunk_stored_but_unacknowledged_is_not_sent_twice(self):
        # The server keeps chunk 2 but answers 503. The uploader must ask what
        # the server has (status query) and move on, not resend and corrupt.
        self.fake.fail_put = {2: "503-stored"}
        data = payload(UNIT * 3)
        self.run_upload(data)
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)
        self.assertEqual(self.fake.data_bytes_received, len(data))

    def test_partial_acknowledgement_resends_only_the_remainder(self):
        self.fake.fail_put = {1: ("partial", 100000)}
        data = payload(UNIT * 2)
        self.run_upload(data)
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)
        self.assertEqual(self.fake.data_bytes_received, len(data))

    def test_retries_are_bounded(self):
        up = self.uploader(max_retries=2)
        session = up.start_session(META, size=UNIT, mime="video/mp4")
        self.fake.always_503 = True
        path = Path(tempfile.mkdtemp()) / "v.mp4"
        self.addCleanup(shutil.rmtree, str(path.parent), True)
        path.write_bytes(payload(UNIT))
        with self.assertRaises(ytupload.UploadError) as ctx:
            up.upload_file(path, session, UNIT)
        self.assertIn("re-run to resume", str(ctx.exception))

    def test_expired_session_raises_session_expired(self):
        up = self.uploader()
        session = up.start_session(META, size=UNIT, mime="video/mp4")
        self.fake.expire_sessions = True
        path = Path(tempfile.mkdtemp()) / "v.mp4"
        self.addCleanup(shutil.rmtree, str(path.parent), True)
        path.write_bytes(payload(UNIT))
        with self.assertRaises(ytupload.SessionExpired):
            up.upload_file(path, session, UNIT)

    def test_expired_token_is_refreshed_once(self):
        self.fake.expected_token = "good"
        self.tokens = Tokens(first="stale", after_refresh="good")
        self.run_upload(payload(UNIT * 2))
        self.assertIn(True, self.tokens.calls)  # asked for a forced refresh
        self.assertEqual(len(bytes(self.fake.only_session()["data"])), UNIT * 2)

    def test_cancel_stops_between_chunks(self):
        event = threading.Event()
        progress = Progress(cancel_event=event)
        self.fake.on_data_put = lambda n: event.set()
        up = self.uploader(progress=progress)
        path = Path(tempfile.mkdtemp()) / "v.mp4"
        self.addCleanup(shutil.rmtree, str(path.parent), True)
        path.write_bytes(payload(UNIT * 4))
        session = up.start_session(META, size=UNIT * 4, mime="video/mp4")
        with self.assertRaises(JobCancelled):
            up.upload_file(path, session, UNIT * 4)
        self.assertEqual(self.fake.data_puts, 1)

    def test_resume_from_offset_uses_the_status_the_server_reports(self):
        data = payload(UNIT * 4)
        path = Path(tempfile.mkdtemp()) / "v.mp4"
        self.addCleanup(shutil.rmtree, str(path.parent), True)
        path.write_bytes(data)
        up = self.uploader()
        session = up.start_session(META, size=len(data), mime="video/mp4")
        # first process dies after 2 chunks
        self.fake.on_data_put = lambda n: None
        with open(str(path), "rb") as fh:
            up._transmit(session, fh.read(UNIT), 0, len(data))
            up._transmit(session, fh.read(UNIT), UNIT, len(data))
        kind, committed, _ = up.probe(session, len(data))
        self.assertEqual((kind, committed), ("incomplete", UNIT * 2))
        before = self.fake.data_bytes_received
        up.upload_file(path, session, len(data), offset=committed)
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)
        self.assertEqual(self.fake.data_bytes_received - before, UNIT * 2)  # only the rest

    def test_probe_returns_none_for_a_dead_session(self):
        up = self.uploader()
        session = up.start_session(META, size=10, mime="video/mp4")
        self.fake.expire_sessions = True
        self.assertIsNone(up.probe(session, 10))

    def test_empty_file_is_refused(self):
        up = self.uploader()
        with self.assertRaises(ytupload.UploadError):
            up.upload_file(__file__, "http://127.0.0.1:1/x", 0)


# --- uploading a stream of unknown length -------------------------------------


class TestUploadStream(_Base):
    def stream(self, data, **kw):
        up = self.uploader(**kw)
        reader = ytupload.ChunkReader(io.BytesIO(data), up.chunk_bytes)
        self.addCleanup(reader.close)
        session = up.start_session(META, size=None, mime="video/mp4")
        return up.upload_stream(reader, session), up, reader

    def test_unknown_length_round_trip(self):
        data = payload(UNIT * 3 + 999)
        (result, sent), _, _ = self.stream(data)
        sess = self.fake.only_session()
        self.assertEqual(bytes(sess["data"]), data)
        self.assertEqual(sent, len(data))
        self.assertIsNone(sess["declared"])  # no X-Upload-Content-Length
        # intermediate chunks say "/*"; only the last states the total
        self.assertEqual([r.rsplit("/", 1)[1] for r in sess["ranges"]], ["*", "*", "*", str(len(data))])

    def test_exact_multiple_of_chunk_still_finishes_with_a_known_total(self):
        data = payload(UNIT * 3)
        (result, sent), _, _ = self.stream(data)
        sess = self.fake.only_session()
        self.assertEqual(bytes(sess["data"]), data)
        self.assertEqual(sess["ranges"][-1].rsplit("/", 1)[1], str(len(data)))
        self.assertTrue(sess["complete"])

    def test_source_smaller_than_one_chunk(self):
        data = payload(5000)
        (result, sent), _, _ = self.stream(data)
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)
        self.assertEqual(result["id"], "vid1")

    def test_stream_survives_a_failed_chunk_without_rewinding_the_source(self):
        self.fake.fail_put = {2: "503-lost", 3: "503-stored"}
        data = payload(UNIT * 4 + 17)
        self.stream(data)
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)
        self.assertEqual(self.fake.data_bytes_received, len(data))

    def test_empty_source_is_an_error(self):
        up = self.uploader()
        reader = ytupload.ChunkReader(io.BytesIO(b""), up.chunk_bytes)
        session = up.start_session(META, size=None, mime="video/mp4")
        with self.assertRaises(ytupload.UploadError):
            up.upload_stream(reader, session)

    def test_unknown_length_refused_at_session_start(self):
        self.fake.reject_unknown_at_init = True
        up = self.uploader()
        with self.assertRaises(ytupload.UnknownLengthUnsupported) as ctx:
            up.start_session(META, size=None, mime="video/mp4")
        self.assertEqual(ctx.exception.pending, [])

    def test_unknown_length_refused_at_first_chunk_hands_back_the_unsent_bytes(self):
        self.fake.reject_unknown_at_chunk = True
        data = payload(UNIT * 4 + 5)
        up = self.uploader()
        reader = ytupload.ChunkReader(io.BytesIO(data), up.chunk_bytes)
        self.addCleanup(reader.close)
        session = up.start_session(META, size=None, mime="video/mp4")
        with self.assertRaises(ytupload.UnknownLengthUnsupported) as ctx:
            up.upload_stream(reader, session)
        # nothing is lost: pending + the rest of the reader is the whole source
        rest = bytearray(b"".join(ctx.exception.pending))
        while True:
            chunk = reader.next_chunk()
            if not chunk:
                break
            rest += chunk
        self.assertEqual(bytes(rest), data)

    def test_a_failing_source_surfaces_before_the_final_chunk_is_confirmed(self):
        class Dies(object):
            def __init__(self):
                self.sent = 0

            def read(self, n):
                if self.sent >= UNIT * 2:
                    raise StageError("ffmpeg stopped")
                self.sent += n
                return payload(n, salt=self.sent)

        up = self.uploader()
        reader = ytupload.ChunkReader(Dies(), up.chunk_bytes)
        self.addCleanup(reader.close)
        session = up.start_session(META, size=None, mime="video/mp4")
        with self.assertRaises(StageError) as ctx:
            up.upload_stream(reader, session)
        self.assertIn("ffmpeg stopped", str(ctx.exception))
        self.assertFalse(self.fake.only_session()["complete"])


class TestChunkReader(unittest.TestCase):
    def test_chunks_then_empty_forever(self):
        reader = ytupload.ChunkReader(io.BytesIO(b"a" * 10), 4)
        self.addCleanup(reader.close)
        self.assertEqual([reader.next_chunk() for _ in range(5)], [b"aaaa", b"aaaa", b"aa", b"", b""])

    def test_short_reads_are_assembled_into_full_chunks(self):
        class Dribble(object):
            def __init__(self):
                self.left = 10

            def read(self, n):
                take = min(self.left, 1)
                self.left -= take
                return b"z" * take

        reader = ytupload.ChunkReader(Dribble(), 4)
        self.addCleanup(reader.close)
        self.assertEqual([reader.next_chunk() for _ in range(4)], [b"zzzz", b"zzzz", b"zz", b""])


# --- the stage ---------------------------------------------------------------


def _settings(**overrides):
    data = {
        "tools": {"yt_dlp": sys.executable, "ffmpeg": sys.executable, "ffprobe": sys.executable},
        "upload": {"youtube": {"chunk_mb": 0.25, "max_retries": 3}},
        "download": {"format": "best", "impersonate": None},
    }
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Settings(data, Path("settings.json"))


class FakeRelay(object):
    """Stands in for FfmpegRelay: a context manager whose .read() serves bytes."""

    def __init__(self, data, fail_at_eof=None):
        self.data, self.fail_at_eof = data, fail_at_eof
        self.argv = None

    def __call__(self, argv, expected_seconds=None, log_path=None):
        self.argv = argv
        self._buf = io.BytesIO(self.data)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        chunk = self._buf.read(n)
        if not chunk and self.fail_at_eof:
            raise StageError(self.fail_at_eof)
        return chunk


class TestUploadStage(_Base):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = Workspace.for_url(self.root, KICK_URL)
        self.ws.update_state(title="Happy OneThree August!!", uploader="gitanshteck",
                             stream_started_at=1786607326)
        self.settings = _settings()
        for patcher in (
            mock.patch.object(youtube_auth, "TokenSource", lambda settings: self.tokens),
            mock.patch.object(ytupload, "UPLOAD_ENDPOINT", self.fake.endpoint),
            mock.patch.object(ytupload.time, "sleep", lambda s: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def vod(self, **info):
        base = {"title": "From Kick", "duration": 100.0, "timestamp": 1786607326,
                "uploader": "gitanshteck", "channel_id": 7}
        base.update(info)
        return upload_stage.VodStream(
            urls=["http://example.invalid/master.m3u8"], headers={}, info=base,
            duration=100.0, approx_bytes=None,
        )

    # -- file mode --

    def test_uploads_the_downloaded_vod_and_records_it(self):
        data = payload(UNIT * 3 + 77)
        (self.ws.root / "video.mp4").write_bytes(data)
        record = upload_stage.upload_vod(self.ws, self.settings)
        sess = self.fake.only_session()
        self.assertEqual(bytes(sess["data"]), data)
        self.assertEqual(sess["metadata"]["status"]["privacyStatus"], "private")  # the default
        self.assertEqual(sess["metadata"]["snippet"]["title"], "Happy OneThree August!!")
        self.assertIn("2026-08-13", sess["metadata"]["snippet"]["description"])
        self.assertEqual(record["video_id"], "vid1")
        self.assertEqual(record["url"], "https://youtu.be/vid1")
        self.assertEqual(record["mode"], "file")
        state = self.ws.read_state()
        self.assertEqual(state["youtube_video_id"], "vid1")
        self.assertTrue(self.ws.stage_done("upload"))
        self.assertIsNone(state.get("upload_session"))

    def test_a_second_run_does_not_upload_again_unless_forced(self):
        (self.ws.root / "video.mp4").write_bytes(payload(UNIT))
        upload_stage.upload_vod(self.ws, self.settings)
        before = len(self.fake.requests)
        again = upload_stage.upload_vod(self.ws, self.settings)
        self.assertEqual(again["video_id"], "vid1")
        self.assertEqual(len(self.fake.requests), before)  # no traffic at all
        forced = upload_stage.upload_vod(self.ws, self.settings, force=True)
        self.assertEqual(forced["video_id"], "vid2")

    def test_privacy_and_title_overrides_reach_youtube(self):
        (self.ws.root / "video.mp4").write_bytes(payload(UNIT))
        upload_stage.upload_vod(self.ws, self.settings, privacy="unlisted",
                                title="Custom <title>", description="Hello")
        meta = self.fake.only_session()["metadata"]
        self.assertEqual(meta["status"]["privacyStatus"], "unlisted")
        self.assertEqual(meta["snippet"]["title"], "Custom ‹title›")
        self.assertEqual(meta["snippet"]["description"], "Hello")

    def test_interrupted_file_upload_resumes_from_the_saved_session(self):
        data = payload(UNIT * 4)
        (self.ws.root / "video.mp4").write_bytes(data)
        # The connection dies from the third chunk on; with 3 retries it gives up.
        self.fake.fail_put = {n: "503-lost" for n in range(3, 30)}
        with self.assertRaises(ytupload.UploadError):
            upload_stage.upload_vod(self.ws, self.settings)
        saved = self.ws.read_state()["upload_session"]
        self.assertTrue(saved["uri"].startswith(self.fake.base))
        self.assertFalse(self.ws.stage_done("upload"))
        received = len(self.fake.only_session()["data"])
        self.assertEqual(received, UNIT * 2)

        # Network is back: a fresh run continues the *same* session.
        self.fake.fail_put = {}
        before = self.fake.data_bytes_received
        record = upload_stage.upload_vod(self.ws, self.settings)
        self.assertEqual(len(self.fake.sessions), 1)  # no second session was created
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)
        self.assertEqual(self.fake.data_bytes_received - before, UNIT * 2)  # only the rest
        self.assertEqual(record["video_id"], "vid1")
        self.assertIsNone(self.ws.read_state().get("upload_session"))

    def test_a_dead_saved_session_starts_a_new_one(self):
        data = payload(UNIT * 2)
        (self.ws.root / "video.mp4").write_bytes(data)
        st = (self.ws.root / "video.mp4").stat()
        self.ws.update_state(upload_session={
            "uri": self.fake.base + "/session/999", "size": st.st_size,
            "mtime": int(st.st_mtime), "file": "video.mp4", "started_at": 0,
        })
        record = upload_stage.upload_vod(self.ws, self.settings)
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)
        self.assertEqual(record["video_id"], "vid1")

    # -- stream mode --

    def test_without_a_local_vod_it_streams_and_writes_nothing_to_disk(self):
        data = payload(UNIT * 3 + 4321)
        relay = FakeRelay(data)
        self.ws.update_state(title=None)  # a bare workspace: no title yet
        with mock.patch.object(upload_stage, "resolve_vod_stream", lambda *a, **k: self.vod()), \
                mock.patch.object(upload_stage, "FfmpegRelay", relay):
            record = upload_stage.upload_vod(self.ws, self.settings)
        sess = self.fake.only_session()
        self.assertEqual(bytes(sess["data"]), data)
        self.assertIsNone(sess["declared"])
        self.assertEqual(record["mode"], "stream")
        self.assertIsNone(self.ws.video_path())          # no VOD was downloaded
        self.assertFalse((self.ws.root / "_upload").exists())  # and no spool
        # the title came from Kick's metadata, adopted into state.json
        self.assertEqual(sess["metadata"]["snippet"]["title"], "From Kick")
        self.assertEqual(self.ws.read_state()["title"], "From Kick")
        self.assertIn("pipe:1", relay.argv)

    def test_refused_unknown_length_falls_back_to_spooling(self):
        self.fake.reject_unknown_at_init = True
        data = payload(UNIT * 3 + 1)
        with mock.patch.object(upload_stage, "resolve_vod_stream", lambda *a, **k: self.vod()), \
                mock.patch.object(upload_stage, "FfmpegRelay", FakeRelay(data)):
            record = upload_stage.upload_vod(self.ws, self.settings)
        sess = self.fake.only_session()
        self.assertEqual(bytes(sess["data"]), data)
        self.assertEqual(sess["declared"], str(len(data)))   # sent with a known size
        self.assertEqual(record["mode"], "stream+spool")
        self.assertFalse((self.ws.root / "_upload" / "spool.mp4").exists())  # cleaned up

    def test_refusal_on_the_first_chunk_loses_no_bytes(self):
        self.fake.reject_unknown_at_chunk = True
        data = payload(UNIT * 4 + 33)
        with mock.patch.object(upload_stage, "resolve_vod_stream", lambda *a, **k: self.vod()), \
                mock.patch.object(upload_stage, "FfmpegRelay", FakeRelay(data)):
            record = upload_stage.upload_vod(self.ws, self.settings)
        # one abandoned session (the refused one) and one that carries the video
        done = [s for s in self.fake.sessions.values() if s["complete"]]
        self.assertEqual(len(done), 1)
        self.assertEqual(bytes(done[0]["data"]), data)
        self.assertEqual(record["mode"], "stream+spool")

    def test_ffmpeg_dying_never_confirms_a_truncated_video(self):
        relay = FakeRelay(payload(UNIT * 2 + 10), fail_at_eof="ffmpeg stopped (exit 1)")
        with mock.patch.object(upload_stage, "resolve_vod_stream", lambda *a, **k: self.vod()), \
                mock.patch.object(upload_stage, "FfmpegRelay", relay):
            with self.assertRaises(StageError) as ctx:
                upload_stage.upload_vod(self.ws, self.settings)
        self.assertIn("ffmpeg stopped", str(ctx.exception))
        self.assertFalse(any(s["complete"] for s in self.fake.sessions.values()))
        self.assertFalse(self.ws.stage_done("upload"))

    def test_explicit_stream_mode_ignores_a_local_vod(self):
        (self.ws.root / "video.mp4").write_bytes(payload(UNIT))
        data = payload(UNIT + 5, salt=9)
        with mock.patch.object(upload_stage, "resolve_vod_stream", lambda *a, **k: self.vod()), \
                mock.patch.object(upload_stage, "FfmpegRelay", FakeRelay(data)):
            record = upload_stage.upload_vod(self.ws, self.settings, mode="stream")
        self.assertEqual(record["mode"], "stream")
        self.assertEqual(bytes(self.fake.only_session()["data"]), data)

    # -- refusals --

    def test_youtube_workspaces_are_refused(self):
        yt = Workspace.for_url(self.root, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        with self.assertRaises(StageError):
            upload_stage.upload_vod(yt, self.settings)

    def test_bad_privacy_and_mode_are_refused_before_any_traffic(self):
        with self.assertRaises(StageError):
            upload_stage.upload_vod(self.ws, self.settings, privacy="everyone")
        with self.assertRaises(StageError):
            upload_stage.upload_vod(self.ws, self.settings, mode="teleport")
        self.assertEqual(self.fake.requests, [])

    def test_missing_credentials_fail_fast(self):
        def boom(settings):
            raise StageError("not authorised")

        (self.ws.root / "video.mp4").write_bytes(payload(UNIT))
        with mock.patch.object(youtube_auth, "TokenSource", boom):
            with self.assertRaises(StageError):
                upload_stage.upload_vod(self.ws, self.settings)
        self.assertEqual(self.fake.requests, [])


# --- pure helpers of the stage -------------------------------------------------


class TestStreamArgv(unittest.TestCase):
    def vod(self, urls, headers=None):
        return upload_stage.VodStream(urls=urls, headers=headers or {}, info={}, duration=1.0, approx_bytes=None)

    def test_single_muxed_input_to_a_pipe_with_stream_copy(self):
        argv = upload_stage.stream_argv("ffmpeg", self.vod(["https://x/a.m3u8"]))
        self.assertEqual(argv[-1], "pipe:1")
        self.assertEqual(argv[argv.index("-c") + 1], "copy")
        self.assertIn("+frag_keyframe+empty_moov+default_base_moof", argv)
        self.assertEqual(argv[argv.index("-f") + 1], "mp4")
        self.assertEqual(argv.count("-i"), 1)
        self.assertIn("pipe:2", argv)  # progress must not share stdout with the media

    def test_separate_video_and_audio_are_mapped_explicitly(self):
        argv = upload_stage.stream_argv("ffmpeg", self.vod(["https://x/v", "https://x/a"]))
        self.assertEqual(argv.count("-i"), 2)
        self.assertIn("1:a:0", argv)

    def test_headers_and_user_agent_are_passed_per_input(self):
        argv = upload_stage.stream_argv(
            "ffmpeg", self.vod(["https://x/a"], {"User-Agent": "UA/1", "Referer": "https://kick.com/"})
        )
        self.assertEqual(argv[argv.index("-user_agent") + 1], "UA/1")
        self.assertEqual(argv[argv.index("-headers") + 1], "Referer: https://kick.com/\r\n")

    def test_options_precede_the_input_they_apply_to(self):
        argv = upload_stage.stream_argv("ffmpeg", self.vod(["https://x/a"], {"Referer": "r"}))
        self.assertLess(argv.index("-headers"), argv.index("-i"))
        self.assertLess(argv.index("-reconnect"), argv.index("-i"))

    def test_adts_aac_from_hls_is_converted_for_mp4_but_not_for_mpegts(self):
        hls = self.vod(["https://x/a.m3u8?token=1"])
        mp4 = upload_stage.stream_argv("ffmpeg", hls, "mp4")
        self.assertEqual(mp4[mp4.index("-bsf:a") + 1], "aac_adtstoasc")
        self.assertNotIn("-bsf:a", upload_stage.stream_argv("ffmpeg", hls, "mpegts"))

    def test_raw_aac_from_a_progressive_file_is_not_given_the_adts_filter(self):
        # aac_adtstoasc errors on AAC that is already in ASC form.
        argv = upload_stage.stream_argv("ffmpeg", self.vod(["https://x/video.mp4"]), "mp4")
        self.assertNotIn("-bsf:a", argv)

    def test_hls_is_recognised_from_the_protocol_when_the_url_has_no_extension(self):
        vod = upload_stage.VodStream(
            urls=["https://x/playlist"], headers={}, duration=1.0, approx_bytes=None,
            info={"protocol": "m3u8_native"},
        )
        self.assertIn("-bsf:a", upload_stage.stream_argv("ffmpeg", vod, "mp4"))

    def test_unknown_container_is_refused(self):
        with self.assertRaises(StageError):
            upload_stage.stream_argv("ffmpeg", self.vod(["u"]), container="avi")

    def test_logs_do_not_keep_signed_url_queries(self):
        text = upload_stage._redact(["ffmpeg", "-i", "https://cdn/x.m3u8?hdnts=SECRET"])
        self.assertNotIn("SECRET", text)


class TestVodStreamFromInfo(unittest.TestCase):
    def test_muxed_format_uses_one_url(self):
        info = {"url": "https://x/a.m3u8", "vcodec": "avc1", "acodec": "mp4a", "duration": 100,
                "tbr": 8000, "http_headers": {"User-Agent": "UA"}}
        vod = upload_stage.vod_stream_from_info(info)
        self.assertEqual(vod.urls, ["https://x/a.m3u8"])
        self.assertEqual(vod.approx_bytes, int(8000 * 1000 / 8 * 100))
        self.assertEqual(vod.headers, {"User-Agent": "UA"})

    def test_separate_audio_is_picked_up(self):
        info = {"requested_formats": [
            {"url": "https://x/v", "vcodec": "avc1", "acodec": "none"},
            {"url": "https://x/a", "vcodec": "none", "acodec": "mp4a"},
        ], "duration": 10}
        self.assertEqual(upload_stage.vod_stream_from_info(info).urls, ["https://x/v", "https://x/a"])

    def test_a_muxed_video_wins_over_a_redundant_audio_track(self):
        info = {"requested_formats": [
            {"url": "https://x/m", "vcodec": "avc1", "acodec": "mp4a"},
            {"url": "https://x/a", "vcodec": "none", "acodec": "mp4a"},
        ]}
        self.assertEqual(upload_stage.vod_stream_from_info(info).urls, ["https://x/m"])

    def test_no_video_is_an_error(self):
        with self.assertRaises(StageError):
            upload_stage.vod_stream_from_info({"url": "u", "vcodec": "none", "acodec": "mp4a"})


# --- the real ffmpeg, when there is one ----------------------------------------


def _find_ffmpeg():
    env = os.environ.get("CLIPBOT_FFMPEG")
    candidates = [env] if env else []
    candidates.append(shutil.which("ffmpeg"))
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


FFMPEG = _find_ffmpeg()
FFPROBE = None
if FFMPEG:
    probe = Path(FFMPEG).with_name("ffprobe" + Path(FFMPEG).suffix)
    FFPROBE = str(probe) if probe.is_file() else shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG, "no ffmpeg (set CLIPBOT_FFMPEG)")
class TestRealFfmpegRelay(unittest.TestCase):
    def test_hls_is_remuxed_to_a_fragmented_mp4_on_a_pipe(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hls = Path(tmp.name)
        subprocess.run(
            [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "10",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac",
             "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
             "-hls_segment_filename", str(hls / "seg%03d.ts"), str(hls / "out.m3u8")],
            check=True,
        )

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(hls), **k)

            def log_message(self, *a):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        url = "http://127.0.0.1:{0}/out.m3u8".format(httpd.server_address[1])
        vod = upload_stage.VodStream(urls=[url], headers={}, info={}, duration=10.0, approx_bytes=None)
        argv = upload_stage.stream_argv(FFMPEG, vod)
        out = bytearray()
        with upload_stage.FfmpegRelay(argv, expected_seconds=10.0) as relay:
            reader = ytupload.ChunkReader(relay, 64 * 1024)
            while True:
                chunk = reader.next_chunk()
                if not chunk:
                    break
                out += chunk
            reader.close()

        self.assertGreater(len(out), 10000)
        self.assertEqual(bytes(out[4:8]), b"ftyp")  # an MP4, written without seeking
        self.assertIn(b"moof", bytes(out))           # fragmented
        if FFPROBE:
            f = Path(tmp.name) / "relayed.mp4"
            f.write_bytes(bytes(out))
            res = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_name",
                 "-of", "default=nw=1", str(f)],
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("codec_name=h264", res.stdout)
            self.assertIn("codec_name=aac", res.stdout)
            duration = float(re.search(r"duration=([\d.]+)", res.stdout).group(1))
            self.assertAlmostEqual(duration, 10.0, delta=1.0)

    def test_a_failing_input_raises_before_the_stream_would_be_confirmed(self):
        vod = upload_stage.VodStream(
            urls=["http://127.0.0.1:9/none.m3u8"], headers={}, info={}, duration=10.0, approx_bytes=None
        )
        argv = upload_stage.stream_argv(FFMPEG, vod)
        with upload_stage.FfmpegRelay(argv, expected_seconds=10.0) as relay:
            with self.assertRaises(StageError):
                relay.read(1024)


if __name__ == "__main__":
    unittest.main()
