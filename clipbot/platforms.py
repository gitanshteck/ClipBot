"""Which streaming platform a VOD URL belongs to, and the ids derived from it.

Kick and YouTube VODs share one pipeline but differ in how their media is
acquired and played, so each workspace records its `platform` in state.json.
Everything that existed before YouTube support is a Kick workspace, which is
why an absent `platform` key means Kick.

Slugs are directory names under `work/`, so `slug_for` must keep producing
exactly what it always has for Kick (tests/test_platforms.py pins that).
YouTube ids are case-sensitive but slugs are lowercase, so the true id is
stored in state.json (`video_id`) and never re-derived from the slug.
"""

import re
from typing import NamedTuple, Optional
from urllib.parse import parse_qs, urlparse

from .utils import slugify

KICK = "kick"
YOUTUBE = "youtube"
PLATFORMS = (KICK, YOUTUBE)
DEFAULT_PLATFORM = KICK

# https://kick.com/<channel>/videos/<uuid>  |  https://kick.com/video/<uuid>
# (moved verbatim from workspace.py)
KICK_VIDEO_RE = re.compile(
    r"kick\.com/(?:video/|(?P<channel>[^/]+)/videos?/)(?P<video_id>[0-9a-zA-Z-]+)",
    re.IGNORECASE,
)

YOUTUBE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_YOUTUBE_HOSTS = frozenset(
    (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    )
)
# /live/<id> is how YouTube links a stream (and its archive) from a channel's
# Live tab; /embed/ and the legacy /v/ carry the id the same way.
_YOUTUBE_ID_IN_PATH = ("live", "embed", "v")


class ParsedUrl(NamedTuple):
    platform: str
    video_id: str
    # What to store as state["url"]: the URL as given for Kick (unchanged
    # behaviour), a clean watch URL for YouTube (drops &t=, &list=, tracking).
    canonical_url: str
    # Kick only: the channel slug, which the Kick workspace slug is built from.
    channel: Optional[str] = None


def parse_url(url: str, platform_hint: Optional[str] = None) -> Optional[ParsedUrl]:
    """Recognise a VOD URL.

    Returns None for anything that isn't a Kick or YouTube video URL (callers
    fall back to their old generic handling). Raises ValueError for a URL that
    is clearly YouTube but not a single video - a channel, playlist or Short -
    with a message fit to show the user.

    `platform_hint="youtube"` additionally accepts a bare 11-character video
    id. It is opt-in because a bare id is ambiguous with arbitrary text (a Kick
    channel slug like "gitanshteck" is also exactly 11 characters).
    """
    text = (url or "").strip()
    if not text:
        return None

    kick = KICK_VIDEO_RE.search(text)
    if kick:
        return ParsedUrl(
            KICK, kick.group("video_id"), text, kick.group("channel") or None
        )

    return _parse_youtube(text, allow_bare_id=(platform_hint == YOUTUBE))


def _parse_youtube(text: str, allow_bare_id: bool) -> Optional[ParsedUrl]:
    if YOUTUBE_ID_RE.match(text):
        return _youtube(text) if allow_bare_id else None

    try:
        parts = urlparse(text if "://" in text else "https://" + text)
        host = (parts.hostname or "").lower()
    except ValueError:  # e.g. an unbalanced "[" - not a URL we recognise
        return None
    if host not in _YOUTUBE_HOSTS:
        return None

    segments = [s for s in parts.path.split("/") if s]
    video_id = None
    if host.endswith("youtu.be"):
        video_id = segments[0] if segments else None
    elif segments and segments[0] == "watch":
        video_id = (parse_qs(parts.query).get("v") or [None])[0]
    elif len(segments) >= 2 and segments[0] in _YOUTUBE_ID_IN_PATH:
        video_id = segments[1]
    else:
        raise ValueError(
            "That looks like a YouTube channel, playlist or Short, not a single "
            "video. Paste a video link (youtube.com/watch?v=..., youtu.be/... or "
            "youtube.com/live/...), or use Browse a channel to pick a stream."
        )

    if not video_id or not YOUTUBE_ID_RE.match(video_id):
        raise ValueError(
            "Couldn't find a YouTube video id in {0!r}.".format(text)
        )
    return _youtube(video_id)


def _youtube(video_id: str) -> ParsedUrl:
    return ParsedUrl(
        YOUTUBE,
        video_id,
        "https://www.youtube.com/watch?v={0}".format(video_id),
        None,
    )


def slug_for(parsed: ParsedUrl) -> str:
    """Workspace directory name. Must stay stable: it is the on-disk identity."""
    if parsed.platform == YOUTUBE:
        # No slugify(): it collapses runs of "-", which YouTube ids can contain,
        # and the id is a fixed 11 characters that are already slug-safe.
        return "yt-" + parsed.video_id.lower()
    return slugify("{0}-{1}".format(parsed.channel or "kick", parsed.video_id))


def platform_of(state: dict) -> str:
    """Platform recorded in a workspace's state.json; absent means Kick, since
    every workspace that predates YouTube support is one."""
    value = (state or {}).get("platform")
    return value if value in PLATFORMS else DEFAULT_PLATFORM
