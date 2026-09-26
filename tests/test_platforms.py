"""Tests for clipbot/platforms.py and the platform-aware bits of workspace.py.

The load-bearing one is the Kick oracle: slugs are directory names under
work/, so adding YouTube support must not change what any existing Kick URL
maps to. `_legacy_slug_for_url` below is the pre-YouTube implementation copied
verbatim; the new function has to agree with it on every non-YouTube input.
"""

import re
import tempfile
import unittest
from pathlib import Path

from clipbot import platforms
from clipbot.utils import StageError, slugify
from clipbot.workspace import (
    MODE_EMBED,
    MODE_LOCAL,
    MODE_NONE,
    Workspace,
    slug_for_url,
)

_LEGACY_KICK_RE = re.compile(
    r"kick\.com/(?:video/|(?P<channel>[^/]+)/videos?/)(?P<video_id>[0-9a-zA-Z-]+)",
    re.IGNORECASE,
)


def _legacy_slug_for_url(url):
    match = _LEGACY_KICK_RE.search(url)
    if match:
        channel = match.group("channel") or "kick"
        video_id = match.group("video_id")
        return slugify("{0}-{1}".format(channel, video_id))
    return slugify(url.rstrip("/").split("/")[-1] or "vod")


# Same pattern the dashboard's get_workspace() enforces on every slug.
SERVER_SLUG_RE = re.compile(r"^[a-z0-9._-]{1,120}$")

KICK_URLS = [
    "https://kick.com/gitanshteck/videos/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21",
    "https://kick.com/GitanshTeck/videos/79AC1495-1F0E-4C1E-8A35-0D2C9B6F4E21",
    "http://kick.com/gitanshteck/video/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21",
    "https://kick.com/video/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21",
    "https://kick.com/video/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21?foo=bar",
    "https://www.kick.com/gitanshteck/videos/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21/",
    "kick.com/gitanshteck/videos/abc-123",
]

# Not Kick and not YouTube: both implementations must fall back identically.
OTHER_URLS = [
    "https://example.com/some/path/clip-name",
    "https://example.com/some/path/",
    "https://twitch.tv/videos/123456789",
    "just-a-slug",
    "",
]


class TestKickSlugsUnchanged(unittest.TestCase):
    def test_matches_legacy_implementation(self):
        for url in KICK_URLS + OTHER_URLS:
            with self.subTest(url=url):
                self.assertEqual(slug_for_url(url), _legacy_slug_for_url(url))

    def test_golden_values(self):
        # Literal values, so a drifting oracle can't hide a drifting slug.
        self.assertEqual(
            slug_for_url(KICK_URLS[0]),
            "gitanshteck-79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21",
        )
        self.assertEqual(
            slug_for_url("https://kick.com/video/79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21"),
            "kick-79ac1495-1f0e-4c1e-8a35-0d2c9b6f4e21",
        )

    def test_kick_url_is_stored_as_given(self):
        parsed = platforms.parse_url(KICK_URLS[0])
        self.assertEqual(parsed.platform, platforms.KICK)
        self.assertEqual(parsed.canonical_url, KICK_URLS[0])
        self.assertEqual(parsed.channel, "gitanshteck")


class TestParseYouTube(unittest.TestCase):
    ID = "dQw4w9WgXcQ"

    def test_every_url_shape_yields_the_same_id(self):
        shapes = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&t=1h2m3s",
            "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&list=PLxyz",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?si=abc&t=30",
            "https://www.youtube.com/live/dQw4w9WgXcQ?si=abc",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "www.youtube.com/watch?v=dQw4w9WgXcQ",
            "youtu.be/dQw4w9WgXcQ",
            "  https://youtu.be/dQw4w9WgXcQ  ",
        ]
        for url in shapes:
            with self.subTest(url=url):
                parsed = platforms.parse_url(url)
                self.assertEqual(parsed.platform, platforms.YOUTUBE)
                self.assertEqual(parsed.video_id, self.ID)
                self.assertEqual(
                    parsed.canonical_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                )

    def test_video_id_case_is_preserved_but_slug_is_lowercase(self):
        parsed = platforms.parse_url("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(parsed.video_id, "dQw4w9WgXcQ")
        self.assertEqual(platforms.slug_for(parsed), "yt-dqw4w9wgxcq")

    def test_slug_keeps_hyphen_runs_and_is_server_safe(self):
        # slugify() would collapse "--"; ids are fixed-length so we must not.
        parsed = platforms.parse_url("https://youtu.be/a--b_C-dEf1")
        self.assertEqual(platforms.slug_for(parsed), "yt-a--b_c-def1")
        self.assertRegex(platforms.slug_for(parsed), SERVER_SLUG_RE)
        self.assertFalse(platforms.slug_for(parsed).startswith("_"))

    def test_slug_for_url_agrees(self):
        self.assertEqual(slug_for_url("https://youtu.be/dQw4w9WgXcQ"), "yt-dqw4w9wgxcq")

    def test_bare_id_needs_the_hint(self):
        self.assertIsNone(platforms.parse_url("dQw4w9WgXcQ"))
        parsed = platforms.parse_url("dQw4w9WgXcQ", platform_hint=platforms.YOUTUBE)
        self.assertEqual(parsed.video_id, "dQw4w9WgXcQ")
        # "gitanshteck" is also exactly 11 characters: it must never be
        # mistaken for a video id unless the caller says it's on the YouTube tab.
        self.assertIsNone(platforms.parse_url("gitanshteck"))

    def test_non_video_youtube_urls_raise_with_a_useful_message(self):
        for url in [
            "https://www.youtube.com/@teck",
            "https://www.youtube.com/@teck/streams",
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
            "https://www.youtube.com/playlist?list=PLxyz",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    platforms.parse_url(url)
                self.assertIn("video", str(ctx.exception).lower())

    def test_watch_url_with_a_bad_or_missing_id_raises(self):
        for url in [
            "https://www.youtube.com/watch",
            "https://www.youtube.com/watch?v=tooshort",
            "https://youtu.be/",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    platforms.parse_url(url)

    def test_unknown_hosts_are_not_youtube(self):
        self.assertIsNone(platforms.parse_url("https://notyoutube.com/watch?v=dQw4w9WgXcQ"))
        self.assertIsNone(platforms.parse_url("https://example.com/"))
        self.assertIsNone(platforms.parse_url(""))
        self.assertIsNone(platforms.parse_url(None))

    def test_slug_for_url_never_raises_on_a_youtube_channel_url(self):
        # Read-only callers (the dashboard's channel listing) rely on this.
        self.assertTrue(slug_for_url("https://www.youtube.com/@teck/streams"))


class TestPlatformOf(unittest.TestCase):
    def test_absent_means_kick(self):
        self.assertEqual(platforms.platform_of({}), platforms.KICK)
        self.assertEqual(platforms.platform_of(None), platforms.KICK)

    def test_recorded_platform_wins(self):
        self.assertEqual(platforms.platform_of({"platform": "youtube"}), platforms.YOUTUBE)
        self.assertEqual(platforms.platform_of({"platform": "kick"}), platforms.KICK)

    def test_unknown_value_falls_back_to_kick(self):
        self.assertEqual(platforms.platform_of({"platform": "twitch"}), platforms.KICK)


class TestWorkspacePlatform(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_for_url_records_youtube_identity(self):
        ws = Workspace.for_url(self.root, "https://youtu.be/dQw4w9WgXcQ?t=5")
        state = ws.read_state()
        self.assertEqual(ws.slug, "yt-dqw4w9wgxcq")
        self.assertEqual(state["platform"], "youtube")
        self.assertEqual(state["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(state["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(ws.platform, "youtube")
        self.assertEqual(ws.video_id, "dQw4w9WgXcQ")

    def test_for_url_records_kick_without_a_video_id(self):
        ws = Workspace.for_url(self.root, KICK_URLS[0])
        state = ws.read_state()
        self.assertEqual(state["platform"], "kick")
        self.assertEqual(state["url"], KICK_URLS[0])
        self.assertNotIn("video_id", state)
        self.assertEqual(ws.slug, _legacy_slug_for_url(KICK_URLS[0]))

    def test_for_url_does_not_overwrite_existing_state(self):
        ws = Workspace.for_url(self.root, KICK_URLS[0])
        ws.update_state(title="kept", platform="kick")
        again = Workspace.for_url(self.root, KICK_URLS[0])
        self.assertEqual(again.read_state()["title"], "kept")

    def test_workspace_without_a_platform_key_is_kick(self):
        # Every workspace on disk today looks like this.
        ws = Workspace(self.root / "old").ensure()
        ws.write_state({"url": KICK_URLS[0], "title": "t"})
        self.assertEqual(ws.platform, "kick")
        self.assertIsNone(ws.video_id)

    def test_for_url_refuses_a_channel_url(self):
        with self.assertRaises(StageError):
            Workspace.for_url(self.root, "https://www.youtube.com/@teck")
        self.assertEqual(list(self.root.iterdir()), [])  # and creates nothing


class TestSourceMode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_youtube_without_video_is_embed(self):
        ws = Workspace.for_url(self.root, "https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(ws.source_mode(), MODE_EMBED)

    def test_a_video_on_disk_makes_it_local(self):
        ws = Workspace.for_url(self.root, "https://youtu.be/dQw4w9WgXcQ")
        (ws.root / "video.mp4").write_bytes(b"x" * 10)
        self.assertEqual(ws.source_mode(), MODE_LOCAL)

    def test_kick_without_video_is_none_not_embed(self):
        ws = Workspace.for_url(self.root, KICK_URLS[0])
        self.assertEqual(ws.source_mode(), MODE_NONE)

    def test_kick_with_video_is_local(self):
        ws = Workspace.for_url(self.root, KICK_URLS[0])
        (ws.root / "video.mp4").write_bytes(b"x" * 10)
        self.assertEqual(ws.source_mode(), MODE_LOCAL)

    def test_missing_workspace_dir_is_none(self):
        self.assertEqual(Workspace(self.root / "nope").source_mode(), MODE_NONE)


class TestSourceAudioPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws").ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_when_absent(self):
        self.assertIsNone(self.ws.source_audio_path())

    def test_finds_the_audio_file(self):
        (self.ws.root / "source_audio.m4a").write_bytes(b"a")
        self.assertEqual(self.ws.source_audio_path().name, "source_audio.m4a")

    def test_ignores_ytdlp_scratch_files(self):
        for name in (
            "source_audio.webm.part",
            "source_audio.webm.ytdl",
            "source_audio.info.json",
            "source_audio.f251.webm",
            "audio.wav",
        ):
            (self.ws.root / name).write_bytes(b"x" * 100)
        self.assertIsNone(self.ws.source_audio_path())

    def test_missing_directory_is_none(self):
        self.assertIsNone(Workspace(Path(self._tmp.name) / "gone").source_audio_path())


if __name__ == "__main__":
    unittest.main()
