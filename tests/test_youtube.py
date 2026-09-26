"""Tests for clipbot/stages/youtube.py, clipbot/ytdlp.py and their wiring into
the audio stage and `download.acquire`.

yt-dlp itself is never run: `run_yt_dlp` is stubbed to write the files a real
run would (video.info.json, source_audio.m4a), so what's tested is the control
flow around it - argv shape, skip/force rules, the live-stream refusal, failure
hints, source-audio cleanup. The one exception is TestRunYtDlp, which runs a
real subprocess (this Python) to prove progress parsing and cancellation work.
"""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from clipbot.config import Settings
from clipbot.progress import JobCancelled, Progress
from clipbot.stages import audio as audio_stage
from clipbot.stages import download as download_stage
from clipbot.stages import youtube as yt
from clipbot.utils import StageError
from clipbot.workspace import Workspace
from clipbot import ytdlp

YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _settings(overrides=None):
    data = {
        # sys.executable stands in for every external binary: it is a real
        # file, so resolve_tool() accepts it without touching PATH.
        "tools": {
            "yt_dlp": sys.executable,
            "ffmpeg": sys.executable,
            "ffprobe": sys.executable,
        },
        "audio": {"sample_rate": 16000, "channels": 1, "codec": "pcm_s16le"},
    }
    for dotted, value in (overrides or {}).items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Settings(data, Path("settings.json"))


def _info(**overrides):
    info = {
        "id": "dQw4w9WgXcQ",
        "title": "Friday stream",
        "channel": "Teck",
        "uploader": "Somebody else",
        "upload_date": "20260925",
        "release_timestamp": 1790000000,
        "timestamp": 1790000999,
        "duration": 16523.0,
        "channel_id": "UCabc",
        "live_status": "was_live",
    }
    info.update(overrides)
    return info


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = Workspace.for_url(self.root, YT_URL)
        self.settings = _settings()

    def tearDown(self):
        self._tmp.cleanup()

    def fake_ytdlp(self, info=None, audio_name="source_audio.m4a", write_audio=True):
        """A stand-in for run_yt_dlp that writes what the real one would, keyed
        off which of our two argv shapes it was handed."""
        calls = []

        def fake(argv, progress, log_path=None):
            calls.append(list(argv))
            if "--skip-download" in argv:
                (self.ws.root / "video.info.json").write_text(
                    json.dumps(info if info is not None else _info()), encoding="utf-8"
                )
            elif write_audio:
                (self.ws.root / audio_name).write_bytes(b"a" * 2048)

        fake.calls = calls
        return fake


class TestArgv(_Base):
    def test_metadata_argv(self):
        argv = yt.metadata_argv(YT_URL, self.ws, self.settings)
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("--skip-download", argv)
        self.assertIn("--write-info-json", argv)
        self.assertIn("--no-playlist", argv)
        self.assertEqual(argv[-1], YT_URL)
        out = argv[argv.index("-o") + 1]
        self.assertEqual(Path(out).name, "video.%(ext)s")  # same info.json name as Kick

    def test_audio_argv(self):
        argv = yt.audio_argv(YT_URL, self.ws, self.settings)
        self.assertEqual(argv[argv.index("-f") + 1], "bestaudio/best")
        out = argv[argv.index("-o") + 1]
        self.assertEqual(Path(out).name, "source_audio.%(ext)s")
        self.assertEqual(Path(out).parent, self.ws.root)
        self.assertEqual(argv[argv.index("--progress-template") + 1], ytdlp.PROGRESS_TEMPLATE)
        self.assertNotIn("--skip-download", argv)

    def test_kick_only_flags_are_never_passed(self):
        # Cloudflare TLS impersonation and the throttle workaround are Kick
        # fixes; they must not leak onto YouTube even when configured.
        settings = _settings({"download.impersonate": "chrome", "download.http_chunk_size": "10M"})
        for argv in (
            yt.metadata_argv(YT_URL, self.ws, settings),
            yt.audio_argv(YT_URL, self.ws, settings),
        ):
            self.assertNotIn("--impersonate", argv)
            self.assertNotIn("--http-chunk-size", argv)

    def test_optional_flags_only_when_configured(self):
        plain = yt.audio_argv(YT_URL, self.ws, self.settings)
        for flag in ("--js-runtimes", "--cookies-from-browser", "--extractor-args"):
            self.assertNotIn(flag, plain)

        configured = _settings(
            {
                "download.youtube.js_runtime": "node",
                "download.youtube.cookies_from_browser": "firefox",
                "download.youtube.player_client": "tv",
                "download.youtube.audio_format": "ba[ext=m4a]",
            }
        )
        argv = yt.audio_argv(YT_URL, self.ws, configured)
        self.assertEqual(argv[argv.index("--js-runtimes") + 1], "node")
        self.assertEqual(argv[argv.index("--cookies-from-browser") + 1], "firefox")
        self.assertEqual(argv[argv.index("--extractor-args") + 1], "youtube:player_client=tv")
        self.assertEqual(argv[argv.index("-f") + 1], "ba[ext=m4a]")

    def test_missing_ytdlp_raises_with_the_install_hint(self):
        from clipbot.utils import ToolMissingError

        settings = _settings({"tools.yt_dlp": "definitely-not-a-real-binary-xyz"})
        with self.assertRaises(ToolMissingError) as ctx:
            yt.audio_argv(YT_URL, self.ws, settings)
        self.assertIn("Deno", str(ctx.exception))


class TestMetadata(_Base):
    def test_metadata_from_info(self):
        fields = yt.metadata_from_info(_info())
        self.assertEqual(fields["title"], "Friday stream")
        self.assertEqual(fields["uploader"], "Teck")  # channel beats uploader
        self.assertEqual(fields["stream_started_at"], 1790000000)  # release beats upload
        self.assertEqual(fields["youtube_duration"], 16523.0)
        self.assertEqual(fields["youtube_live_status"], "was_live")

    def test_falls_back_to_timestamp_and_uploader(self):
        fields = yt.metadata_from_info(
            _info(channel=None, release_timestamp=None)
        )
        self.assertEqual(fields["uploader"], "Somebody else")
        self.assertEqual(fields["stream_started_at"], 1790000999)

    def test_fetch_records_state_and_seeds_duration(self):
        fake = self.fake_ytdlp()
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            yt.fetch_metadata(YT_URL, self.ws, self.settings)
        state = self.ws.read_state()
        self.assertEqual(state["title"], "Friday stream")
        self.assertEqual(state["youtube_duration"], 16523.0)
        self.assertEqual(state["duration"], 16523.0)  # seeded: none probed yet

    def test_fetch_never_overwrites_the_probed_duration(self):
        # The audio stage's probe owns `duration`; YouTube's figure must not clobber it.
        self.ws.update_state(duration=16492.0)
        fake = self.fake_ytdlp()
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            yt.fetch_metadata(YT_URL, self.ws, self.settings)
        state = self.ws.read_state()
        self.assertEqual(state["duration"], 16492.0)
        self.assertEqual(state["youtube_duration"], 16523.0)

    def test_cached_info_json_is_reused(self):
        fake = self.fake_ytdlp()
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            yt.fetch_metadata(YT_URL, self.ws, self.settings)
            yt.fetch_metadata(YT_URL, self.ws, self.settings)
            self.assertEqual(len(fake.calls), 1)
            yt.fetch_metadata(YT_URL, self.ws, self.settings, force=True)
            self.assertEqual(len(fake.calls), 2)

    def test_info_written_while_live_is_not_trusted_later(self):
        # A cache from when the stream was live would pin `is_live` forever.
        (self.ws.root / "video.info.json").write_text(
            json.dumps(_info(live_status="is_live")), encoding="utf-8"
        )
        fake = self.fake_ytdlp(info=_info(live_status="was_live"))
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            fields = yt.fetch_metadata(YT_URL, self.ws, self.settings)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fields["youtube_live_status"], "was_live")

    def test_success_without_an_info_file_is_an_error(self):
        with mock.patch.object(yt, "run_yt_dlp"):
            with self.assertRaises(StageError):
                yt.fetch_metadata(YT_URL, self.ws, self.settings)


class TestFetchAudio(_Base):
    def test_happy_path(self):
        fake = self.fake_ytdlp()
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            path = yt.fetch_audio(YT_URL, self.ws, self.settings)
        self.assertEqual(path.name, "source_audio.m4a")
        self.assertEqual(len(fake.calls), 2)  # metadata, then audio
        stage = self.ws.read_state()["stages"]["download"]
        self.assertTrue(stage["audio_only"])
        self.assertEqual(stage["source_audio"], "source_audio.m4a")
        self.assertEqual(stage["bytes"], 2048)

    def test_url_in_state_stays_canonical(self):
        fake = self.fake_ytdlp()
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            yt.fetch_audio(YT_URL + "&t=99s", self.ws, self.settings)
        self.assertEqual(self.ws.read_state()["url"], YT_URL)

    def test_existing_source_audio_is_skipped(self):
        (self.ws.root / "source_audio.webm").write_bytes(b"x")
        with mock.patch.object(yt, "run_yt_dlp") as run:
            path = yt.fetch_audio(YT_URL, self.ws, self.settings)
        run.assert_not_called()
        self.assertEqual(path.name, "source_audio.webm")

    def test_extracted_wav_means_nothing_to_fetch(self):
        self.ws.audio_path.write_bytes(b"RIFF")
        with mock.patch.object(yt, "run_yt_dlp") as run:
            path = yt.fetch_audio(YT_URL, self.ws, self.settings)
        run.assert_not_called()
        self.assertEqual(path, self.ws.audio_path)

    def test_force_never_hands_back_the_wav(self):
        # extract_audio(force=True) would otherwise read audio.wav as its own
        # input and write over it.
        self.ws.audio_path.write_bytes(b"RIFF")
        fake = self.fake_ytdlp()
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            path = yt.fetch_audio(YT_URL, self.ws, self.settings, force=True)
        self.assertEqual(path.name, "source_audio.m4a")
        self.assertEqual(len(fake.calls), 2)

    def test_force_replaces_an_existing_source(self):
        (self.ws.root / "source_audio.webm").write_bytes(b"old")
        fake = self.fake_ytdlp()
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            path = yt.fetch_audio(YT_URL, self.ws, self.settings, force=True)
        self.assertEqual(path.name, "source_audio.m4a")
        self.assertFalse((self.ws.root / "source_audio.webm").exists())

    def test_refuses_a_stream_that_is_live_right_now(self):
        fake = self.fake_ytdlp(info=_info(live_status="is_live"))
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            with self.assertRaises(StageError) as ctx:
                yt.fetch_audio(YT_URL, self.ws, self.settings)
        self.assertIn("live right now", str(ctx.exception))
        self.assertEqual(len(fake.calls), 1)  # metadata only - never fetched audio
        self.assertIsNone(self.ws.source_audio_path())

    def test_refuses_a_scheduled_stream(self):
        fake = self.fake_ytdlp(info=_info(live_status="is_upcoming"))
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            with self.assertRaises(StageError) as ctx:
                yt.fetch_audio(YT_URL, self.ws, self.settings)
        self.assertIn("hasn't started", str(ctx.exception))

    def test_post_live_warns_but_continues(self):
        fake = self.fake_ytdlp(info=_info(live_status="post_live"))
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            with self.assertLogs("clipbot.stages.youtube", level="WARNING"):
                path = yt.fetch_audio(YT_URL, self.ws, self.settings)
        self.assertEqual(path.name, "source_audio.m4a")

    def test_success_without_an_audio_file_is_an_error(self):
        fake = self.fake_ytdlp(write_audio=False)
        with mock.patch.object(yt, "run_yt_dlp", side_effect=fake):
            with self.assertRaises(StageError):
                yt.fetch_audio(YT_URL, self.ws, self.settings)

    def test_bot_check_failure_carries_the_cookies_hint(self):
        def boom(argv, progress, log_path=None):
            raise StageError("ERROR: Sign in to confirm you\u2019re not a bot")

        with mock.patch.object(yt, "run_yt_dlp", side_effect=boom):
            with self.assertRaises(StageError) as ctx:
                yt.fetch_audio(YT_URL, self.ws, self.settings)
        self.assertIn("cookies_from_browser", str(ctx.exception))


class TestExplainFailure(unittest.TestCase):
    def test_known_failures_get_a_fix(self):
        cases = {
            "Sign in to confirm you\u2019re not a bot": "cookies_from_browser",
            "ERROR: [youtube] x: The page needs to be reloaded.": "Deno",
            "WARNING: YouTube is forcing SABR streaming for this client": "Deno",
            "n challenge solving failed": "Deno",
            "Requested format is not available": "Deno",
            "ERROR: Private video. Sign in if you've been granted access": "private",
            "Video unavailable": "unavailable",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                self.assertIn(needle.lower(), yt.explain_failure(text).lower())

    def test_unknown_failure_adds_nothing(self):
        self.assertEqual(yt.explain_failure("something entirely different"), "")
        self.assertEqual(yt.explain_failure(""), "")
        self.assertEqual(yt.explain_failure(None), "")


class TestDropSourceAudio(_Base):
    def test_deletes_only_its_own_source_audio(self):
        own = self.ws.root / "source_audio.m4a"
        own.write_bytes(b"x" * 10)
        self.assertTrue(yt.drop_source_audio(self.ws, own, self.settings))
        self.assertFalse(own.exists())

    def test_keep_setting_wins(self):
        own = self.ws.root / "source_audio.m4a"
        own.write_bytes(b"x")
        settings = _settings({"download.youtube.keep_source_audio": True})
        self.assertFalse(yt.drop_source_audio(self.ws, own, settings))
        self.assertTrue(own.exists())

    def test_never_deletes_a_file_passed_in_from_elsewhere(self):
        (self.ws.root / "source_audio.m4a").write_bytes(b"x")
        elsewhere = self.root / "my-recording.mp4"
        elsewhere.write_bytes(b"y")
        self.assertFalse(yt.drop_source_audio(self.ws, elsewhere, self.settings))
        self.assertTrue(elsewhere.exists())

    def test_nothing_to_delete(self):
        self.assertFalse(yt.drop_source_audio(self.ws, self.ws.root / "nope.m4a", self.settings))


class TestAudioStageHandoff(_Base):
    """extract_audio picks up the YouTube source and cleans it up; Kick is untouched."""

    def _fake_ffmpeg(self, argv, **kwargs):
        Path(argv[-1]).write_bytes(b"RIFF" + b"\0" * 64)
        self.ffmpeg_inputs.append(argv[argv.index("-i") + 1])

    def setUp(self):
        super().setUp()
        self.ffmpeg_inputs = []
        patches = [
            mock.patch.object(audio_stage, "run_command", side_effect=self._fake_ffmpeg),
            mock.patch.object(audio_stage, "probe_duration", return_value=16492.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_youtube_extracts_from_source_audio_then_deletes_it(self):
        src = self.ws.root / "source_audio.m4a"
        src.write_bytes(b"a" * 100)
        out = audio_stage.extract_audio(self.ws, self.settings)
        self.assertEqual(out, self.ws.audio_path)
        self.assertEqual(self.ffmpeg_inputs, [str(src)])
        self.assertFalse(src.exists())
        self.assertEqual(self.ws.read_state()["duration"], 16492.0)  # probe owns it

    def test_keep_source_audio_keeps_it(self):
        src = self.ws.root / "source_audio.m4a"
        src.write_bytes(b"a" * 100)
        audio_stage.extract_audio(self.ws, _settings({"download.youtube.keep_source_audio": True}))
        self.assertTrue(src.exists())

    def test_explicit_video_argument_still_wins(self):
        (self.ws.root / "source_audio.m4a").write_bytes(b"a")
        other = self.root / "elsewhere.mkv"
        other.write_bytes(b"v")
        audio_stage.extract_audio(self.ws, self.settings, video=other)
        self.assertEqual(self.ffmpeg_inputs, [str(other)])
        self.assertTrue(other.exists())  # a caller's file is never deleted

    def test_youtube_without_source_audio_says_to_fetch_it(self):
        with self.assertRaises(StageError) as ctx:
            audio_stage.extract_audio(self.ws, self.settings)
        self.assertIn("Fetch audio", str(ctx.exception))

    def test_kick_still_extracts_from_the_video_and_keeps_it(self):
        kick = Workspace.for_url(
            self.root, "https://kick.com/gitanshteck/videos/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21"
        )
        video = kick.root / "video.mp4"
        video.write_bytes(b"v" * 100)
        audio_stage.extract_audio(kick, self.settings)
        self.assertEqual(self.ffmpeg_inputs, [str(video)])
        self.assertTrue(video.exists())

    def test_kick_without_a_video_keeps_the_original_message(self):
        kick = Workspace.for_url(
            self.root, "https://kick.com/gitanshteck/videos/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21"
        )
        with self.assertRaises(StageError) as ctx:
            audio_stage.extract_audio(kick, self.settings)
        self.assertIn("cleanup stage", str(ctx.exception))


class TestAcquire(_Base):
    def test_youtube_routes_to_the_audio_fetch(self):
        with mock.patch.object(yt, "fetch_audio", return_value=Path("a.m4a")) as fetch, \
                mock.patch.object(download_stage, "download_vod") as vod:
            out = download_stage.acquire(YT_URL, self.ws, self.settings, force=True, quality="720")
        fetch.assert_called_once()
        self.assertTrue(fetch.call_args.kwargs["force"])
        vod.assert_not_called()
        self.assertEqual(out, Path("a.m4a"))

    def test_kick_routes_to_the_vod_download_with_quality(self):
        kick = Workspace.for_url(
            self.root, "https://kick.com/gitanshteck/videos/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21"
        )
        with mock.patch.object(download_stage, "download_vod", return_value=Path("v.mp4")) as vod, \
                mock.patch.object(yt, "fetch_audio") as fetch:
            download_stage.acquire("https://kick.com/x", kick, self.settings, quality="720")
        fetch.assert_not_called()
        self.assertEqual(vod.call_args.kwargs["quality"], "720")

    def test_workspace_without_a_platform_key_is_kick(self):
        legacy = Workspace(self.root / "old").ensure()
        legacy.write_state({"url": "https://kick.com/a/videos/b"})
        with mock.patch.object(download_stage, "download_vod", return_value=Path("v.mp4")) as vod:
            download_stage.acquire("https://kick.com/a/videos/b", legacy, self.settings)
        vod.assert_called_once()


class TestListChannelStreams(_Base):
    def _run(self, stdout="", returncode=0, stderr=""):
        proc = subprocess.CompletedProcess([], returncode, stdout, stderr)
        return mock.patch.object(yt.subprocess, "run", return_value=proc)

    def test_parses_entries(self):
        payload = {
            "entries": [
                {"id": "dQw4w9WgXcQ", "title": "Friday", "duration": 16523, "view_count": 42,
                 "live_status": "was_live"},
                {"id": "aaaaaaaaaaa", "title": "Live now", "live_status": "is_live"},
                {"title": "no id - skipped"},
                "not-a-dict",
            ]
        }
        with self._run(json.dumps(payload)) as run:
            streams = yt.list_channel_streams("@teck", self.settings, limit=5)
        self.assertEqual([s["video_id"] for s in streams], ["dQw4w9WgXcQ", "aaaaaaaaaaa"])
        first = streams[0]
        self.assertEqual(first["url"], YT_URL)
        self.assertEqual(first["duration_seconds"], 16523)
        self.assertEqual(first["thumbnail"], "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg")
        self.assertEqual(streams[1]["live_status"], "is_live")

        argv = run.call_args.args[0]
        self.assertIn("--flat-playlist", argv)
        self.assertNotIn("--no-playlist", argv)  # the listing IS a playlist
        self.assertEqual(argv[argv.index("--playlist-end") + 1], "5")
        self.assertEqual(argv[-1], "https://www.youtube.com/@teck/streams")

    def test_handle_is_validated(self):
        for bad in ("", "  ", "../etc", "a b", "x" * 61, "a/b", "a;b"):
            with self.subTest(handle=bad):
                with self._run("{}") as run:
                    with self.assertRaises(StageError):
                        yt.list_channel_streams(bad, self.settings)
                run.assert_not_called()

    def test_failure_is_reported_with_a_hint(self):
        with self._run(returncode=1, stderr="ERROR: The page needs to be reloaded."):
            with self.assertRaises(StageError) as ctx:
                yt.list_channel_streams("teck", self.settings)
        self.assertIn("Deno", str(ctx.exception))

    def test_timeout_is_reported(self):
        with mock.patch.object(
            yt.subprocess, "run", side_effect=subprocess.TimeoutExpired("yt-dlp", 1)
        ):
            with self.assertRaises(StageError) as ctx:
                yt.list_channel_streams("teck", self.settings, timeout=1)
        self.assertIn("timed out", str(ctx.exception))

    def test_unreadable_json_is_reported(self):
        with self._run("<html>"):
            with self.assertRaises(StageError):
                yt.list_channel_streams("teck", self.settings)


class TestHealth(unittest.TestCase):
    def test_yt_dlp_age(self):
        import time

        released = time.mktime((2026, 8, 19, 0, 0, 0, 0, 0, -1))
        self.assertEqual(yt.yt_dlp_age_days("2026.08.19", today=released + 38 * 86400 + 5), 38)
        self.assertIsNone(yt.yt_dlp_age_days("not-a-version"))
        self.assertIsNone(yt.yt_dlp_age_days(""))

    def _checks(self, settings, versions, which=None):
        """Run health_checks with tool versions/PATH faked: `versions` maps
        the first argv element's basename to `--version` output."""

        def fake_version(argv):
            return versions.get(Path(argv[0]).name.lower().replace(".exe", ""))

        which = which or {}
        with mock.patch.object(yt, "_tool_version", side_effect=fake_version), \
                mock.patch.object(yt.shutil, "which", side_effect=lambda n: which.get(n)):
            return {c["name"]: c for c in yt.health_checks(settings)}

    def test_fresh_yt_dlp_and_deno_are_ok(self):
        import time

        today = time.strftime("%Y.%m.%d")
        py = Path(sys.executable).name.lower().replace(".exe", "")
        checks = self._checks(
            _settings(),
            {py: "Deprecated Feature: py3.9\n" + today, "deno": "deno 2.6.6 (stable)\nv8 14"},
            which={"deno": "C:/deno.exe"},
        )
        self.assertTrue(checks["yt-dlp version"]["ok"])
        self.assertTrue(checks["JS runtime (Deno)"]["ok"])
        self.assertIn("2.6.6", checks["JS runtime (Deno)"]["detail"])

    def test_old_yt_dlp_is_flagged_with_the_update_command(self):
        py = Path(sys.executable).name.lower().replace(".exe", "")
        checks = self._checks(_settings(), {py: "2025.10.14"}, which={})
        self.assertFalse(checks["yt-dlp version"]["ok"])
        self.assertIn("days old", checks["yt-dlp version"]["detail"])
        self.assertIn("pip install -U", checks["yt-dlp version"]["detail"])

    def test_missing_deno_says_how_to_install_it(self):
        py = Path(sys.executable).name.lower().replace(".exe", "")
        checks = self._checks(_settings(), {py: "2026.08.19"}, which={})
        self.assertFalse(checks["JS runtime (Deno)"]["ok"])
        self.assertIn("winget install DenoLand.Deno", checks["JS runtime (Deno)"]["detail"])

    def test_deno_below_the_minimum_fails(self):
        py = Path(sys.executable).name.lower().replace(".exe", "")
        checks = self._checks(
            _settings(), {py: "2026.08.19", "deno": "deno 2.1.0"}, which={"deno": "C:/deno"}
        )
        self.assertFalse(checks["JS runtime (Deno)"]["ok"])
        self.assertIn("2.3.0", checks["JS runtime (Deno)"]["detail"])

    def test_node_needs_22_and_must_be_selected(self):
        py = Path(sys.executable).name.lower().replace(".exe", "")
        node_settings = _settings({"download.youtube.js_runtime": "node"})
        old = self._checks(node_settings, {py: "2026.08.19", "node": "v20.19.2"}, {"node": "C:/node"})
        self.assertFalse(old["JS runtime (Node)"]["ok"])
        new = self._checks(node_settings, {py: "2026.08.19", "node": "v22.4.0"}, {"node": "C:/node"})
        self.assertTrue(new["JS runtime (Node)"]["ok"])
        # Node 22 on PATH does not help while yt-dlp is left on its Deno default.
        default = self._checks(_settings(), {py: "2026.08.19", "node": "v22.4.0"}, {"node": "C:/node"})
        self.assertNotIn("JS runtime (Node)", default)
        self.assertFalse(default["JS runtime (Deno)"]["ok"])

    def test_missing_yt_dlp_reports_a_single_failing_check(self):
        checks = yt.health_checks(_settings({"tools.yt_dlp": "no-such-binary-xyz"}))
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["ok"])


class TestProbe(unittest.TestCase):
    def _probe(self, stdout, returncode=0):
        proc = subprocess.CompletedProcess([], returncode, stdout, None)
        with mock.patch.object(yt.subprocess, "run", return_value=proc):
            return yt.probe_youtube(_settings())

    def test_success_needs_an_audio_only_format(self):
        ok = self._probe("ID  EXT  RESOLUTION\n140 m4a  audio only\n18  mp4  640x360")
        self.assertTrue(ok["ok"])
        self.assertIn("audio-only available", ok["detail"])
        no_audio = self._probe("ID  EXT  RESOLUTION\n18  mp4  640x360")
        self.assertFalse(no_audio["ok"])

    def test_failure_reports_the_last_line_and_a_hint(self):
        result = self._probe("WARNING: SABR\nERROR: [youtube] x: The page needs to be reloaded.", 1)
        self.assertFalse(result["ok"])
        self.assertIn("page needs to be reloaded", result["detail"])
        self.assertIn("Deno", result["detail"])

    def test_timeout(self):
        with mock.patch.object(
            yt.subprocess, "run", side_effect=subprocess.TimeoutExpired("yt-dlp", 1)
        ):
            result = yt.probe_youtube(_settings(), timeout=1)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["detail"])


class TestRunYtDlp(unittest.TestCase):
    """Runs a real subprocess (this Python) in place of yt-dlp."""

    def _progress(self, cancel=None):
        events = []
        prog = Progress(sink=lambda kind, payload: events.append((kind, payload)),
                        cancel_event=cancel, min_interval=0.0)
        return prog, events

    def test_progress_lines_become_updates(self):
        script = (
            "print('[youtube] extracting')\n"
            "print('CLIPBOT_PROGRESS 1024|4096|NA|512.5')\n"
            "print('CLIPBOT_PROGRESS 4096|4096|NA|NA')\n"
        )
        prog, events = self._progress()
        ytdlp.run_yt_dlp([sys.executable, "-c", script], prog)
        updates = [p for kind, p in events if kind == "progress"]
        self.assertEqual([u["current"] for u in updates], [1024.0, 4096.0])
        self.assertEqual(updates[0]["total"], 4096.0)
        self.assertEqual(updates[0]["rate"], 512.5)
        self.assertIsNone(updates[1]["rate"])  # yt-dlp's "NA"
        self.assertEqual(updates[1]["unit"], "bytes")

    def test_a_second_pass_starts_a_new_phase(self):
        # yt-dlp downloads video then audio; downloaded_bytes resets between them.
        script = (
            "print('CLIPBOT_PROGRESS 900|1000|NA|1')\n"
            "print('CLIPBOT_PROGRESS 50|200|NA|1')\n"
        )
        prog, events = self._progress()
        ytdlp.run_yt_dlp([sys.executable, "-c", script], prog)
        self.assertEqual(len([e for e in events if e[0] == "phase"]), 2)

    def test_malformed_progress_lines_are_ignored(self):
        script = "print('CLIPBOT_PROGRESS garbage')\nprint('CLIPBOT_PROGRESS a|b|c|d')\n"
        prog, events = self._progress()
        ytdlp.run_yt_dlp([sys.executable, "-c", script], prog)
        self.assertEqual([e for e in events if e[0] == "progress"], [])

    def test_failure_carries_the_output_tail(self):
        script = "import sys\nprint('ERROR: boom')\nsys.exit(3)\n"
        prog, _ = self._progress()
        with self.assertRaises(StageError) as ctx:
            ytdlp.run_yt_dlp([sys.executable, "-c", script], prog)
        self.assertIn("exit 3", str(ctx.exception))
        self.assertIn("ERROR: boom", str(ctx.exception))

    def test_cancel_terminates_the_process(self):
        script = "import time\nprint('started', flush=True)\ntime.sleep(60)\n"
        cancel = threading.Event()
        cancel.set()
        prog, _ = self._progress(cancel)
        started = __import__("time").time()
        with self.assertRaises(JobCancelled):
            ytdlp.run_yt_dlp([sys.executable, "-c", script], prog)
        self.assertLess(__import__("time").time() - started, 20)

    def test_log_file_records_the_command_and_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "logs" / "download.log"
            prog, _ = self._progress()
            ytdlp.run_yt_dlp([sys.executable, "-c", "print('hello tail')"], prog, log_path=log_path)
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("hello tail", text)


if __name__ == "__main__":
    unittest.main()
