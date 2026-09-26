"""Tests for clipbot/ytsegments.py and its use by the cut and compile stages.

Nothing here touches the network or runs yt-dlp/ffmpeg: `resolve_streams`
(one yt-dlp extraction) and `run_ffmpeg`/`fetch_segment` are stubbed, so what is
tested is the control flow and argv shape around them - the bounded-request
options that fix the measured throttling hang, URL expiry and retry, the
fingerprint rules, and that **local** cut/compile behaviour and fingerprints are
byte-identical to what they were before YouTube segments existed.
"""

import hashlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipbot import compilations as comp
from clipbot import review
from clipbot import ytsegments
from clipbot.config import Settings
from clipbot.progress import JobCancelled
from clipbot.stages import compile as compile_stage
from clipbot.stages import cut as cut_stage
from clipbot.utils import StageError, ToolMissingError
from clipbot.workspace import Workspace

YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _settings(overrides=None, work_root=None):
    data = {
        # sys.executable stands in for every external binary: it is a real
        # file, so resolve_tool() accepts it without touching PATH.
        "tools": {"ffmpeg": sys.executable, "yt_dlp": sys.executable},
        "cut": {
            "pad_start": 1.0, "pad_end": 1.5, "min_duration": 0.5,
            "encoder": "libx264", "preset": "veryfast", "crf": 20,
        },
        "compile": {"min_duration": 0.5, "encoder": "libx264", "preset": "slow", "crf": 18},
    }
    if work_root:
        data["work_root"] = str(work_root)
    for dotted, value in (overrides or {}).items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Settings(data, Path("settings.json"))


def _streams(format_ids="136+140", width=1280, height=720, fps=30.0, lifetime=6 * 3600,
             audio=True, headers=None):
    expire = int(time.time() + lifetime)
    # The format ids ride along in the URLs so two different resolutions build
    # visibly different ffmpeg argvs (a retry test relies on telling them apart).
    return ytsegments.Streams(
        video_url="https://gv.example/videoplayback?itag=136&fmt={0}&expire={1}".format(
            format_ids, expire),
        audio_url=("https://gv.example/videoplayback?itag=140&fmt={0}&expire={1}".format(
            format_ids, expire) if audio else None),
        headers=headers if headers is not None else {},
        width=width, height=height, fps=fps,
        expires_at=float(expire), format_ids=format_ids,
    )


def _info_format(**kw):
    fmt = {"url": "https://gv.example/x?expire=%d" % (time.time() + 3600), "protocol": "https",
           "format_id": "0", "vcodec": "none", "acodec": "none"}
    fmt.update(kw)
    return fmt


class TestStreamsFromInfo(unittest.TestCase):
    def test_video_plus_audio(self):
        info = {"requested_formats": [
            _info_format(format_id="136", vcodec="avc1.4d401f", width=1280, height=720, fps=30,
                         http_headers={"User-Agent": "UA", "Accept": "*/*"},
                         url="https://gv/v?expire=2000000000"),
            _info_format(format_id="140", acodec="mp4a.40.2", url="https://gv/a?expire=1999999000"),
        ]}
        s = ytsegments._streams_from_info(info)
        self.assertEqual(s.video_url, "https://gv/v?expire=2000000000")
        self.assertEqual(s.audio_url, "https://gv/a?expire=1999999000")
        self.assertEqual(s.format_ids, "136+140")
        self.assertEqual((s.width, s.height, s.fps), (1280, 720, 30))
        self.assertEqual(s.headers, {"User-Agent": "UA", "Accept": "*/*"})
        self.assertEqual(s.expires_at, 1999999000.0)  # the sooner of the two

    def test_single_muxed_format(self):
        info = _info_format(format_id="18", vcodec="avc1", acodec="mp4a", width=640, height=360)
        s = ytsegments._streams_from_info(info)
        self.assertIsNone(s.audio_url)
        self.assertEqual(s.format_ids, "18")

    def test_a_segmented_stream_is_refused_with_an_explanation(self):
        for protocol in ("m3u8_native", "http_dash_segments"):
            info = {"requested_formats": [
                _info_format(vcodec="avc1", protocol=protocol),
                _info_format(acodec="mp4a"),
            ]}
            with self.assertRaises(StageError) as ctx:
                ytsegments._streams_from_info(info)
            self.assertIn("segmented", str(ctx.exception))

    def test_missing_audio_or_video_is_an_error(self):
        with self.assertRaises(StageError):
            ytsegments._streams_from_info({"requested_formats": [_info_format(vcodec="avc1")]})
        with self.assertRaises(StageError):
            ytsegments._streams_from_info({"requested_formats": [_info_format(acodec="mp4a")]})

    def test_expiry_parsing(self):
        self.assertEqual(ytsegments._expiry("https://x/y?a=1&expire=1790430153&b=2"), 1790430153.0)
        soon = ytsegments._expiry("https://x/y?a=1")
        self.assertAlmostEqual(soon, time.time() + ytsegments.FALLBACK_LIFETIME_SECONDS, delta=5)


class TestRequestSize(unittest.TestCase):
    def test_default_and_clamping(self):
        self.assertEqual(ytsegments.request_size(_settings()), 1024 * 1024)
        # 16 MiB requests were measured throttled to a crawl: never allow them.
        self.assertEqual(
            ytsegments.request_size(_settings({"download.youtube.request_size": 16 * 1024 * 1024})),
            8 * 1024 * 1024)
        self.assertEqual(
            ytsegments.request_size(_settings({"download.youtube.request_size": 10})), 64 * 1024)
        self.assertEqual(
            ytsegments.request_size(_settings({"download.youtube.request_size": "junk"})), 1024 * 1024)

    def test_default_format_is_progressive_https_only(self):
        # An HLS/DASH-manifest format can't be range-read like this, so every
        # part of every fallback alternative must demand plain https.
        parts = [
            part
            for alternative in ytsegments.DEFAULT_SEGMENT_FORMAT.split("/")
            for part in alternative.split("+")
        ]
        self.assertGreaterEqual(len(parts), 4)
        for part in parts:
            self.assertIn("[protocol=https]", part)
        self.assertNotIn("m3u8", ytsegments.DEFAULT_SEGMENT_FORMAT)


class TestSegmentArgv(unittest.TestCase):
    def _argv(self, streams=None, **kw):
        return ytsegments.segment_argv(
            "ffmpeg", streams or _streams(), kw.pop("start", 100.5), kw.pop("end", 130.0),
            Path("out.mp4"), "libx264", "slow", 18, **kw)

    def test_every_input_uses_bounded_requests_and_seeks_before_its_input(self):
        argv = self._argv(size=2097152)
        inputs = [i for i, a in enumerate(argv) if a == "-i"]
        self.assertEqual(len(inputs), 2)
        for i in inputs:
            window = argv[max(0, i - 8):i]
            self.assertIn("-request_size", window)
            self.assertEqual(window[window.index("-request_size") + 1], "2097152")
            self.assertIn("-multiple_requests", window)
            self.assertEqual(argv[i - 2], "-ss")  # input seek: -ss immediately before -i
            self.assertEqual(argv[i - 1], "00:01:40.500")

    def test_maps_and_encoder(self):
        argv = self._argv()
        self.assertEqual(argv[argv.index("-map") + 1], "0:v:0")
        self.assertIn("1:a:0", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "29.500")
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertEqual(argv[argv.index("-preset") + 1], "slow")
        self.assertEqual(argv[argv.index("-crf") + 1], "18")
        self.assertEqual(argv[argv.index("-pix_fmt") + 1], "yuv420p")
        self.assertNotIn("copy", argv)                   # never a stream copy
        self.assertEqual(argv[-1], "out.mp4")
        self.assertIn("pipe:1", argv)                    # progress for run_ffmpeg

    def test_all_inputs_come_before_the_output_duration(self):
        argv = self._argv()
        self.assertLess(max(i for i, a in enumerate(argv) if a == "-i"), argv.index("-t"))

    def test_muxed_stream_is_a_single_input(self):
        argv = self._argv(_streams(audio=False))
        self.assertEqual(argv.count("-i"), 1)
        self.assertIn("0:a:0", argv)
        self.assertNotIn("1:a:0", argv)

    def test_headers_are_passed_with_crlf_and_the_user_agent_separately(self):
        argv = self._argv(_streams(headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}))
        first_input = argv.index("-i")
        self.assertEqual(argv[argv.index("-user_agent") + 1], "Mozilla/5.0")
        headers = argv[argv.index("-headers") + 1]
        self.assertEqual(headers, "Accept-Language: en\r\n")
        self.assertLess(argv.index("-headers"), first_input)

    def test_no_headers_means_no_header_flags(self):
        argv = self._argv(_streams(headers={}))
        self.assertNotIn("-headers", argv)
        self.assertNotIn("-user_agent", argv)


class TestResolveStreams(unittest.TestCase):
    def _run(self, stdout="", returncode=0, stderr=""):
        proc = subprocess.CompletedProcess([], returncode, stdout, stderr)
        return mock.patch.object(ytsegments.subprocess, "run", return_value=proc)

    def test_parses_and_asks_for_the_configured_format(self):
        import json
        info = {"requested_formats": [
            _info_format(format_id="136", vcodec="avc1"), _info_format(format_id="140", acodec="mp4a")]}
        with self._run(json.dumps(info)) as run:
            s = ytsegments.resolve_streams(YT_URL, _settings({"download.youtube.js_runtime": "node"}))
        self.assertEqual(s.format_ids, "136+140")
        argv = run.call_args.args[0]
        self.assertIn("-J", argv)
        self.assertEqual(argv[argv.index("-f") + 1], ytsegments.DEFAULT_SEGMENT_FORMAT)
        self.assertEqual(argv[-1], YT_URL)
        self.assertEqual(argv[argv.index("--js-runtimes") + 1], "node")   # shared YouTube flags apply
        self.assertNotIn("--impersonate", argv)

    def test_failure_carries_the_hint(self):
        with self._run(returncode=1, stderr="ERROR: The page needs to be reloaded."):
            with self.assertRaises(StageError) as ctx:
                ytsegments.resolve_streams(YT_URL, _settings())
        self.assertIn("Deno", str(ctx.exception))

    def test_timeout_and_bad_json(self):
        with mock.patch.object(ytsegments.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("yt-dlp", 1)):
            with self.assertRaises(StageError) as ctx:
                ytsegments.resolve_streams(YT_URL, _settings(), timeout=1)
        self.assertIn("timed out", str(ctx.exception))
        with self._run("<html>"):
            with self.assertRaises(StageError):
                ytsegments.resolve_streams(YT_URL, _settings())


class TestStreamResolver(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace.for_url(Path(self._tmp.name), YT_URL)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_lazily_once_and_reuses(self):
        with mock.patch.object(ytsegments, "resolve_streams", return_value=_streams()) as resolve:
            r = ytsegments.StreamResolver(self.ws, _settings())
            resolve.assert_not_called()                      # nothing at construction
            r.get(); r.get(); r.tag()
            self.assertEqual(resolve.call_count, 1)
            r.get(refresh=True)
            self.assertEqual(resolve.call_count, 2)

    def test_re_resolves_shortly_before_expiry(self):
        soon = _streams(lifetime=ytsegments.REFRESH_MARGIN_SECONDS - 60)
        with mock.patch.object(ytsegments, "resolve_streams", side_effect=[soon, _streams()]) as resolve:
            r = ytsegments.StreamResolver(self.ws, _settings())
            r.get()
            r.get()
            self.assertEqual(resolve.call_count, 2)

    def test_tag_names_the_video_and_the_formats(self):
        with mock.patch.object(ytsegments, "resolve_streams", return_value=_streams("137+140")):
            tag = ytsegments.StreamResolver(self.ws, _settings()).tag()
        self.assertEqual(tag, "yt:dQw4w9WgXcQ:137+140")      # the case-sensitive id, not the slug

    def test_a_workspace_without_a_url_is_an_error(self):
        state = self.ws.read_state(); state.pop("url"); self.ws.write_state(state)
        with self.assertRaises(StageError):
            ytsegments.StreamResolver(self.ws, _settings()).get()


class TestFetchSegment(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace.for_url(Path(self._tmp.name), YT_URL)
        self.out = self.ws.root / "seg.mp4"
        self.settings = _settings()

    def tearDown(self):
        self._tmp.cleanup()

    def _fetch(self, resolver, run):
        with mock.patch.object(ytsegments, "run_ffmpeg", side_effect=run):
            ytsegments.fetch_segment(resolver, 100.0, 130.0, self.out, self.settings,
                                     "libx264", "slow", 18, base=2, span=1.0, label="seg")

    def _resolver(self, *stream_results):
        r = ytsegments.StreamResolver(self.ws, self.settings)
        patcher = mock.patch.object(ytsegments, "resolve_streams", side_effect=list(stream_results))
        self.resolve = patcher.start()
        self.addCleanup(patcher.stop)
        return r

    def test_success_runs_ffmpeg_once_with_progress_mapping(self):
        seen = []

        def run(argv, total, progress, base, span, log_path, out_path, label=None):
            seen.append((total, base, span, label))
            Path(out_path).write_bytes(b"x")

        self._fetch(self._resolver(_streams()), run)
        self.assertEqual(seen, [(30.0, 2, 1.0, "seg")])
        self.assertTrue(self.out.exists())
        self.assertEqual(self.resolve.call_count, 1)

    def test_a_rejected_url_is_re_resolved_and_retried_once(self):
        attempts = []

        def run(argv, total, progress, base, span, log_path, out_path, label=None):
            attempts.append(argv)
            if len(attempts) == 1:
                Path(out_path).write_bytes(b"partial")
                raise StageError("ffmpeg failed: HTTP error 403 Forbidden")
            Path(out_path).write_bytes(b"whole")

        self._fetch(self._resolver(_streams("136+140"), _streams("137+140")), run)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.resolve.call_count, 2)               # fresh URLs for the retry
        self.assertEqual(self.out.read_bytes(), b"whole")           # partial output was cleared
        self.assertNotEqual(attempts[0], attempts[1])               # it really used the new streams

    def test_it_only_retries_once(self):
        def run(argv, *a, **k):
            raise StageError("HTTP error 403 Forbidden")

        with self.assertRaises(StageError):
            self._fetch(self._resolver(_streams(), _streams(), _streams()), run)
        self.assertEqual(self.resolve.call_count, 2)               # not an endless loop

    def test_other_failures_are_not_retried(self):
        calls = []

        def run(argv, *a, **k):
            calls.append(1)
            raise StageError("Conversion failed!")

        with self.assertRaises(StageError):
            self._fetch(self._resolver(_streams()), run)
        self.assertEqual(len(calls), 1)

    def test_an_old_ffmpeg_gets_an_actionable_message(self):
        def run(argv, *a, **k):
            raise StageError("Unrecognized option 'request_size'.")

        with self.assertRaises(StageError) as ctx:
            self._fetch(self._resolver(_streams()), run)
        self.assertIn("8.1", str(ctx.exception))

    def test_cancel_propagates_without_a_retry(self):
        calls = []

        def run(argv, *a, **k):
            calls.append(1)
            raise JobCancelled("cancelled by request")

        with self.assertRaises(JobCancelled):
            self._fetch(self._resolver(_streams()), run)
        self.assertEqual(len(calls), 1)

    def test_a_missing_ffmpeg_is_reported(self):
        self.settings = _settings({"tools.ffmpeg": "no-such-ffmpeg-binary"})
        with self.assertRaises(ToolMissingError):
            self._fetch(self._resolver(_streams()), lambda *a, **k: None)


class TestFfmpegSupport(unittest.TestCase):
    def _probe(self, stdout):
        proc = subprocess.CompletedProcess([], 0, stdout, None)
        with mock.patch.object(ytsegments.subprocess, "run", return_value=proc):
            return ytsegments.ffmpeg_supports_request_size(_settings())

    def test_detects_the_option(self):
        self.assertTrue(self._probe("https AVOptions:\n  -request_size <int64> ...\n"))
        self.assertFalse(self._probe("https AVOptions:\n  -seekable <boolean> ...\n"))

    def test_unrunnable_ffmpeg_is_none_not_an_exception(self):
        self.assertIsNone(ytsegments.ffmpeg_supports_request_size(
            _settings({"tools.ffmpeg": "no-such-ffmpeg-binary"})))


# --------------------------------------------------------------------------
# cut
# --------------------------------------------------------------------------


def _fake_fetch(calls):
    def fake(resolver, start, end, out_path, settings, *args, **kwargs):
        calls.append({"start": start, "end": end, "out": Path(out_path), "args": args, "kw": kwargs})
        Path(out_path).write_bytes(b"fake-clip")
    return fake


class TestCutFromYouTube(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace.for_url(Path(self._tmp.name), YT_URL)
        self.ws.update_state(duration=1000.0)
        self.settings = _settings()
        self.calls = []
        self.streams = _streams()
        for patcher in (
            mock.patch.object(ytsegments, "fetch_segment", side_effect=_fake_fetch(self.calls)),
            mock.patch.object(ytsegments, "resolve_streams", side_effect=lambda *a, **k: self.streams),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _clip(self, start=100.0, end=130.0, title="first"):
        return review.add_manual_clip(self.ws, start, end, title=title)

    def test_cuts_straight_from_youtube_and_records_the_clip(self):
        clip = self._clip()
        out_dir = cut_stage.cut_clips(self.ws, self.settings)
        self.assertEqual(out_dir, self.ws.clips_dir)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual((call["start"], call["end"]), (99.0, 131.5))    # padded like a local cut
        self.assertTrue(call["out"].exists())
        self.assertEqual(call["kw"]["encoder"], "libx264")
        self.assertEqual(call["kw"]["preset"], "veryfast")               # cut.*, not compile.*
        self.assertEqual(call["kw"]["crf"], 20)

        saved = review.get_clip(review.load(self.ws), clip["id"])
        self.assertEqual(saved["status"], review.STATUS_CUT)
        self.assertTrue(saved["output"]["re_encode"])                    # never a stream copy
        self.assertTrue(saved["output"]["fingerprint"].endswith("|yt:dQw4w9WgXcQ:136+140"))
        self.assertEqual(saved["output"]["file"], "clips/" + call["out"].name)

    def test_an_unchanged_clip_is_skipped_and_force_recuts(self):
        self._clip()
        cut_stage.cut_clips(self.ws, self.settings)
        cut_stage.cut_clips(self.ws, self.settings)
        self.assertEqual(len(self.calls), 1)
        cut_stage.cut_clips(self.ws, self.settings, force=True)
        self.assertEqual(len(self.calls), 2)

    def test_a_better_resolution_of_the_same_video_recuts(self):
        # e.g. HD finishing processing after a first cut at 720p.
        self._clip()
        cut_stage.cut_clips(self.ws, self.settings)
        self.streams = _streams("137+140", height=1080)
        cut_stage.cut_clips(self.ws, self.settings)
        self.assertEqual(len(self.calls), 2)

    def test_a_failed_clip_is_marked_and_the_rest_still_cut(self):
        first = self._clip(100.0, 130.0, "one")
        second = self._clip(300.0, 330.0, "two")
        original = ytsegments.fetch_segment.side_effect

        def flaky(resolver, start, end, out_path, settings, *args, **kwargs):
            if start < 200:
                raise StageError("stream refused")
            original(resolver, start, end, out_path, settings, *args, **kwargs)

        ytsegments.fetch_segment.side_effect = flaky
        cut_stage.cut_clips(self.ws, self.settings)
        doc = review.load(self.ws)
        self.assertEqual(review.get_clip(doc, first["id"])["status"], review.STATUS_FAILED)
        self.assertIn("stream refused", review.get_clip(doc, first["id"])["notes"])
        self.assertEqual(review.get_clip(doc, second["id"])["status"], review.STATUS_CUT)

    def test_every_clip_failing_raises(self):
        self._clip()
        ytsegments.fetch_segment.side_effect = StageError("nope")
        with self.assertRaises(StageError) as ctx:
            cut_stage.cut_clips(self.ws, self.settings)
        self.assertIn("Every clip failed", str(ctx.exception))

    def test_cancel_removes_the_partial_file_and_propagates(self):
        self._clip()

        def cancelled(resolver, start, end, out_path, settings, *a, **k):
            Path(out_path).write_bytes(b"partial")
            raise JobCancelled("cancelled by request")

        ytsegments.fetch_segment.side_effect = cancelled
        with self.assertRaises(JobCancelled):
            cut_stage.cut_clips(self.ws, self.settings)
        self.assertEqual(list(self.ws.clips_dir.glob("*.mp4")), [])

    def test_nothing_is_resolved_when_there_are_no_approved_clips(self):
        # Resolving costs a yt-dlp extraction (seconds): don't pay it for nothing.
        clip = self._clip()
        review.update_clip(self.ws, clip["id"], {"status": review.STATUS_REJECTED})
        out_dir = cut_stage.cut_clips(self.ws, self.settings)
        self.assertEqual(out_dir, self.ws.clips_dir)
        ytsegments.resolve_streams.assert_not_called()
        self.assertEqual(self.calls, [])


class TestCutStaysLocalForLocalVideos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.kick = Workspace.for_url(root, "https://kick.com/gitanshteck/videos/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21")
        self.kick.update_state(duration=1000.0)
        self.settings = _settings()

    def tearDown(self):
        self._tmp.cleanup()

    def test_local_fingerprint_is_unchanged(self):
        s = self.settings
        self.assertEqual(cut_stage._fingerprint(1.0, 2.0, s, False), "1.000|2.000|1.0|1.5|copy")
        self.assertEqual(cut_stage._fingerprint(1.0, 2.0, s, True), "1.000|2.000|1.0|1.5|encode")
        self.assertEqual(cut_stage._fingerprint(1.0, 2.0, s, True, "yt:x:1+2"),
                         "1.000|2.000|1.0|1.5|encode|yt:x:1+2")

    def test_a_local_video_is_still_stream_copied_and_youtube_is_never_touched(self):
        (self.kick.root / "video.mp4").write_bytes(b"fake-source")
        review.add_manual_clip(self.kick, 100.0, 130.0, title="c")
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            Path(argv[-1]).write_bytes(b"fake")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(cut_stage, "run_command", side_effect=fake_run), \
                mock.patch.object(ytsegments, "fetch_segment") as fetch, \
                mock.patch.object(ytsegments, "resolve_streams") as resolve:
            cut_stage.cut_clips(self.kick, self.settings)
        fetch.assert_not_called()
        resolve.assert_not_called()
        self.assertIn("copy", seen[0])

    def test_a_kick_workspace_without_a_video_keeps_the_original_error(self):
        review.add_manual_clip(self.kick, 100.0, 130.0, title="c")
        with self.assertRaises(StageError) as ctx:
            cut_stage.cut_clips(self.kick, self.settings)
        self.assertIn("cleanup stage", str(ctx.exception))


# --------------------------------------------------------------------------
# compile
# --------------------------------------------------------------------------


def _legacy_segment_fingerprint(start, end, slug, settings):
    """The pre-YouTube implementation, verbatim."""
    encoder, preset, crf = compile_stage._encode_settings(settings)
    blob = "{0}|{1:.3f}|{2:.3f}|{3}|{4}|{5}".format(slug, start, end, encoder, preset, crf)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _legacy_compilation_fingerprint(name, resolved, settings):
    """The pre-YouTube implementation, verbatim."""
    encoder, preset, crf = compile_stage._encode_settings(settings)
    parts = [name] + [
        "{0}:{1:.3f}-{2:.3f}".format(slug, start, end) for slug, start, end in resolved
    ]
    parts += [encoder, preset, str(crf),
              str(settings.get("cut.pad_start", 1.0)), str(settings.get("cut.pad_end", 1.5))]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


class TestLocalCompileFingerprintsUnchanged(unittest.TestCase):
    """Existing cached renders (scratch files named by these hashes) stay valid
    only while a local segment's fingerprint is byte-identical to before."""

    def test_matches_the_original_implementation(self):
        s = _settings()
        for args in [(10.0, 20.0, "ws"), (0.0, 5.5, "gitanshteck-79ac"), (99.999, 200.001, "a")]:
            self.assertEqual(compile_stage._segment_fingerprint(*args, settings=s),
                             _legacy_segment_fingerprint(*args, settings=s))
        for name, resolved in [("n", [("ws", 0.0, 5.0)]), ("x", [("a", 1.0, 2.0), ("b", 3.0, 4.0)])]:
            self.assertEqual(compile_stage._compilation_fingerprint(name, resolved, s),
                             _legacy_compilation_fingerprint(name, resolved, s))

    def test_golden_values(self):
        s = _settings()
        self.assertEqual(compile_stage._segment_fingerprint(10.0, 20.0, "ws", s), "c6d4e66d6c86")
        self.assertEqual(
            compile_stage._compilation_fingerprint("name", [("ws", 0.0, 5.0)], s), "451ae5be992115f2")

    def test_a_youtube_tag_changes_both(self):
        s = _settings()
        plain = compile_stage._segment_fingerprint(10.0, 20.0, "ws", s)
        tagged = compile_stage._segment_fingerprint(10.0, 20.0, "ws", s, "yt:v:136+140")
        retagged = compile_stage._segment_fingerprint(10.0, 20.0, "ws", s, "yt:v:137+140")
        self.assertEqual(len({plain, tagged, retagged}), 3)
        base = compile_stage._compilation_fingerprint("n", [("ws", 0.0, 5.0)], s)
        self.assertNotEqual(base, compile_stage._compilation_fingerprint("n", [("ws", 0.0, 5.0)], s, "yt:v:1"))


def _fake_join(argv, *args, **kwargs):
    Path(argv[-1]).write_bytes(b"fake-mp4-bytes")


class TestCompileFromYouTube(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(work_root=self.root)
        self.ws = Workspace.for_url(self.root, YT_URL)
        self.ws.update_state(duration=1000.0)
        self.calls = []
        self.streams = _streams()
        for patcher in (
            mock.patch.object(ytsegments, "fetch_segment", side_effect=_fake_fetch(self.calls)),
            mock.patch.object(ytsegments, "resolve_streams", side_effect=lambda *a, **k: self.streams),
            mock.patch.object(compile_stage, "_run_ffmpeg", side_effect=_fake_join),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _define(self, name="yt", segments=None):
        comp.upsert(self.ws, name, segments or [
            {"start": 100.0, "end": 110.0, "label": "one"},
            {"start": 200.0, "end": 210.0, "label": "two"},
        ], self.settings)

    def test_fetches_each_segment_from_youtube_and_joins_them(self):
        self._define()
        out = compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertTrue(out.exists())
        self.assertEqual([(c["start"], c["end"]) for c in self.calls], [(99.0, 111.5), (199.0, 211.5)])
        # compile.* encode settings, positionally: encoder, preset, crf
        self.assertEqual(self.calls[0]["args"], ("libx264", "slow", 18))
        scratch = list(self.ws.compile_scratch_dir("yt").glob("*.mp4"))
        self.assertEqual(len(scratch), 2)
        listing = (self.ws.compile_scratch_dir("yt") / "list.ffconcat").read_text(encoding="utf-8")
        self.assertEqual(listing.count("file "), 2)
        rendered = comp.get(comp.load(self.ws), "yt")
        self.assertAlmostEqual(rendered["output"]["duration"], 25.0, places=2)
        self.assertTrue(self.ws.stage_done("compile"))

    def test_an_unchanged_compilation_fetches_nothing_more(self):
        self._define()
        compile_stage.render_compilation(self.ws, self.settings, "yt")
        compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertEqual(len(self.calls), 2)

    def test_adding_a_segment_fetches_only_the_new_one(self):
        self._define()
        compile_stage.render_compilation(self.ws, self.settings, "yt")
        self._define(segments=[
            {"start": 100.0, "end": 110.0, "label": "one"},
            {"start": 200.0, "end": 210.0, "label": "two"},
            {"start": 300.0, "end": 310.0, "label": "three"},
        ])
        compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]["start"], 299.0)

    def test_a_better_resolution_redoes_everything(self):
        self._define()
        compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.streams = _streams("137+140", height=1080)
        compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertEqual(len(self.calls), 4)

    def test_cancel_removes_the_partial_segment(self):
        self._define()

        def cancelled(resolver, start, end, out_path, *a, **k):
            Path(out_path).write_bytes(b"partial")
            raise JobCancelled("cancelled by request")

        ytsegments.fetch_segment.side_effect = cancelled
        with self.assertRaises(JobCancelled):
            compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertEqual(list(self.ws.compile_scratch_dir("yt").glob("*.mp4")), [])

    def test_mixing_youtube_and_local_segments_is_refused(self):
        kick = Workspace.for_url(self.root, "https://kick.com/gitanshteck/videos/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21")
        (kick.root / "video.mp4").write_bytes(b"fake-source")
        kick.update_state(duration=1000.0, video_file="video.mp4")
        self._define(segments=[
            {"start": 100.0, "end": 110.0, "label": "yt"},
            {"start": 100.0, "end": 110.0, "label": "kick", "slug": kick.slug},
        ])
        with self.assertRaises(StageError) as ctx:
            compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertIn("mixes", str(ctx.exception))
        self.assertEqual(self.calls, [])                     # refused before any fetching

    def test_segments_from_videos_with_different_shapes_are_refused(self):
        other = Workspace.for_url(self.root, "https://youtu.be/abcdefghijk")
        other.update_state(duration=1000.0)
        by_video = {"dQw4w9WgXcQ": _streams(width=1920, height=1080, fps=60.0),
                    "abcdefghijk": _streams(width=1280, height=720, fps=30.0)}

        def resolve(url, settings, *a, **k):
            return by_video[url.split("=")[-1] if "=" in url else url.rsplit("/", 1)[-1]]

        ytsegments.resolve_streams.side_effect = resolve
        self._define(segments=[
            {"start": 100.0, "end": 110.0, "label": "a"},
            {"start": 100.0, "end": 110.0, "label": "b", "slug": other.slug},
        ])
        with self.assertRaises(StageError) as ctx:
            compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertIn("1920x1080", str(ctx.exception))
        self.assertIn("1280x720", str(ctx.exception))

    def test_segments_from_two_videos_with_the_same_shape_are_allowed(self):
        other = Workspace.for_url(self.root, "https://youtu.be/abcdefghijk")
        other.update_state(duration=1000.0)
        self._define(segments=[
            {"start": 100.0, "end": 110.0, "label": "a"},
            {"start": 100.0, "end": 110.0, "label": "b", "slug": other.slug},
        ])
        out = compile_stage.render_compilation(self.ws, self.settings, "yt")
        self.assertTrue(out.exists())
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
