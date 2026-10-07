"""Fetching just a time range of a YouTube video, straight off YouTube.

A YouTube workspace has no video on disk (stages/youtube.py fetches audio only),
so "Cut approved" and the compile page's Render can't cut from a local file.
This module cuts from YouTube itself: only the requested seconds cross the
network (a 10 s segment 1000 s into a video took ~1 s).

How it works, and why it is not simply `yt-dlp --download-sections`
(measured 2026-09-26 with yt-dlp 2026.08.19 and ffmpeg 8.1.2):

* yt-dlp resolves the stream once (`-J -f <selector>`), which yields direct
  googlevideo URLs for a video-only and an audio-only **progressive HTTPS**
  stream. HLS/DASH-manifest formats are excluded by the selector: they can't be
  range-read the same way.
* ffmpeg can seek those URLs (`-ss` before `-i` turns into HTTP range requests)
  **only if it is told to use bounded requests.** By default it opens ONE
  unbounded range (`Range: bytes=0-`) and to seek it "soft-seeks", draining the
  rest of that response. googlevideo throttles unbounded ranges to ~31 KB/s
  (bounded ones ran at 4-12 MB/s, even 15 MB into the file), so draining a 21 MB
  file took minutes: a 30-second `--download-sections` sat at 0 % CPU for over
  five minutes. `-request_size N -multiple_requests 1` makes every request
  bounded, and the same seeks took 0.4 s (audio) and 1.2 s (video, 1000 s in).
  yt-dlp's section download doesn't pass those options, so this module drives
  ffmpeg itself. N is clamped to 8 MiB: 16 MiB requests were throttled to a
  crawl again, and 1 MiB measured fastest. `-request_size` first shipped in
  ffmpeg 8.1 (absent from 8.0 and 7.x, checked against libavformat/http.c at
  each tag); an older build fails immediately on the unknown option and
  `fetch_segment` says why.
* Segments are always **re-encoded** (a precise `-ss` decodes from the previous
  keyframe and drops frames up to the requested time). That is frame-exact,
  needs no special handling of the two separate inputs, and joins cleanly, and it
  ran at ~9-10x realtime here (720p30, x264 slow, crf 18). A stream-copy
  ("fast") cut was measured too and is not offered: it snaps to keyframes that
  were 3.7-7 s apart on the test video, carries hidden pre-roll frames that
  surface as timestamp collisions when joined with the concat demuxer, and gives
  each clip a silent lead-in - see the spike notes in CLAUDE.md.
* Stream URLs expire (~6 h, `expire=` in the URL) and are bound to the
  requester's IP. `StreamResolver` re-resolves shortly before expiry, and
  `fetch_segment` retries once with fresh URLs if ffmpeg reports a 4xx.
* A dropped or stalled connection must not become a broken clip. ffmpeg treats
  a response that ends early as the end of that input: against a local server
  that cuts one range short it logs "Stream ends prematurely ... Error during
  demuxing: I/O error", **exits 0**, and writes a file whose video track is a
  fraction of the audio's (1.4 s of 10 s). The container still reports 10 s, so
  only the per-track durations show it. On a real 3.5 h 1080p60 stream one
  segment came back with no video track at all and was reported as a success.
  So every input carries `-reconnect` (recovers from drops; measured clean
  through four in a row) plus `-rw_timeout` (a stalled read gives up after 20 s
  instead of hanging; alone that only truncates silently, together with
  -reconnect it recovers), and `fetch_segment` then checks the output's own
  tracks against the source's (googlevideo URLs carry each track's length as
  `dur=`) and fetches once more if anything is missing or short.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .ffrun import run_ffmpeg
from .progress import NULL_PROGRESS, Progress
from .stages import youtube as youtube_stage
from .utils import StageError, format_timestamp, get_logger, resolve_tool
from .workspace import Workspace

log = get_logger(__name__)

FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add "
    "the bin folder to PATH or set tools.ffmpeg in config/settings.json."
)

# Progressive-HTTPS only (see the module docstring). Prefer H.264 + AAC (fast to
# decode), fall back to anything progressive, then to a muxed single stream.
DEFAULT_SEGMENT_FORMAT = (
    "bv*[vcodec^=avc1][height<=1080][protocol=https]+ba[acodec^=mp4a][protocol=https]"
    "/bv*[height<=1080][protocol=https]+ba[protocol=https]"
    "/b[height<=1080][protocol=https]"
    "/b[protocol=https]"
)

DEFAULT_REQUEST_SIZE = 1024 * 1024
MIN_REQUEST_SIZE = 64 * 1024
MAX_REQUEST_SIZE = 8 * 1024 * 1024

# ffmpeg gives up on a network read that has produced nothing for this long
# (microseconds) and, with -reconnect, tries again. Without it a dead connection
# hangs the job - and its Cancel button, which is only checked when ffmpeg
# prints a progress line - until the OS gives up.
READ_TIMEOUT_US = 20 * 1000 * 1000
RECONNECT_DELAY_MAX = 5

# Re-resolve when the URLs have less than this long to live.
REFRESH_MARGIN_SECONDS = 600
# Used when a URL carries no `expire=` (never seen, but don't trust it).
FALLBACK_LIFETIME_SECONDS = 3600

# ffmpeg's wording when a googlevideo URL has expired or been refused.
_REJECTED_MARKERS = ("403", "forbidden", "404 not found", "410 gone", "server returned 4")


class Streams(NamedTuple):
    video_url: str
    # None when yt-dlp chose a single muxed format (video and audio in one URL).
    audio_url: Optional[str]
    headers: Dict[str, str]
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    expires_at: float
    format_ids: str
    # Each track's own length in seconds, from the `dur=` in its googlevideo
    # URL. A complete segment can't be longer than what the track has left, so a
    # clip at the very end of the stream isn't mistaken for a truncated one.
    video_duration: Optional[float] = None
    audio_duration: Optional[float] = None


def _expiry(url: str) -> float:
    try:
        return float(parse_qs(urlparse(url).query)["expire"][0])
    except (KeyError, IndexError, ValueError):
        return time.time() + FALLBACK_LIFETIME_SECONDS


def _url_duration(url: Optional[str]) -> Optional[float]:
    """A googlevideo URL's `dur=`: how long that track is, in seconds."""
    if not url:
        return None
    try:
        return float(parse_qs(urlparse(url).query)["dur"][0])
    except (KeyError, IndexError, ValueError):
        return None


def segment_format(settings: Settings) -> str:
    return str(settings.get("download.youtube.segment_format") or DEFAULT_SEGMENT_FORMAT)


def request_size(settings: Settings) -> int:
    """Bounded HTTP request size, clamped: 16 MiB requests were measured
    throttled to a crawl, so a mistyped setting can't recreate the hang."""
    try:
        size = int(settings.get("download.youtube.request_size") or DEFAULT_REQUEST_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_REQUEST_SIZE
    return max(MIN_REQUEST_SIZE, min(MAX_REQUEST_SIZE, size))


def resolve_streams(url: str, settings: Settings, timeout: int = 180) -> Streams:
    """One yt-dlp extraction: the direct URLs for the best progressive pair."""
    argv = youtube_stage.base_argv(settings) + ["-J", "-f", segment_format(settings), url]
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise StageError("Asking YouTube for the stream URLs timed out after {0}s.".format(timeout))
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-1500:]
        hint = youtube_stage.explain_failure(detail)
        raise StageError(
            "yt-dlp couldn't resolve {0}:\n{1}{2}".format(
                url, detail, "\n\n" + hint if hint else ""
            )
        )
    try:
        info = json.loads(proc.stdout)
    except ValueError:
        raise StageError("yt-dlp returned unreadable JSON while resolving {0}.".format(url))
    return _streams_from_info(info)


def _has(fmt: Dict[str, Any], key: str) -> bool:
    return fmt.get(key) not in (None, "none")


def _streams_from_info(info: Dict[str, Any]) -> Streams:
    formats = info.get("requested_formats") or [info]
    video = next((f for f in formats if _has(f, "vcodec")), None)
    if video is None or not video.get("url"):
        raise StageError("YouTube offered no video stream for this video.")
    if _has(video, "acodec"):
        audio = None  # one muxed format carries both
    else:
        audio = next((f for f in formats if _has(f, "acodec") and not _has(f, "vcodec")), None)
        if audio is None or not audio.get("url"):
            raise StageError("YouTube offered no audio stream for this video.")

    for fmt in [video] + ([audio] if audio else []):
        if fmt.get("protocol") not in ("https", "http"):
            raise StageError(
                "YouTube only offered a segmented ({0}) stream for this video, which can't "
                "be range-read. A stream that has only just ended is still being processed - "
                "try again later.".format(fmt.get("protocol"))
            )

    headers = {
        str(k): str(v)
        for k, v in (video.get("http_headers") or info.get("http_headers") or {}).items()
    }
    ids = "+".join(str(f.get("format_id")) for f in [video] + ([audio] if audio else []))
    return Streams(
        video_url=video["url"],
        audio_url=audio["url"] if audio else None,
        headers=headers,
        width=video.get("width"),
        height=video.get("height"),
        fps=video.get("fps"),
        expires_at=min(_expiry(video["url"]), _expiry(audio["url"]) if audio else float("inf")),
        format_ids=ids,
        video_duration=_url_duration(video["url"]),
        # One muxed URL carries both tracks, so its `dur=` is the audio's too.
        audio_duration=_url_duration(audio["url"] if audio else video["url"]),
    )


class StreamResolver(object):
    """Resolves a workspace's stream URLs lazily, once, and again when they are
    about to expire or after a rejection. One per cut/compile job."""

    def __init__(self, ws: Workspace, settings: Settings):
        self.ws = ws
        self.settings = settings
        state = ws.read_state()
        self.video_id = state.get("video_id") or ws.slug
        self.url = state.get("url")
        self._streams = None  # type: Optional[Streams]

    def get(self, refresh: bool = False) -> Streams:
        stale = self._streams is None or (
            self._streams.expires_at - time.time() < REFRESH_MARGIN_SECONDS
        )
        if refresh or stale:
            if not self.url:
                raise StageError("No YouTube URL recorded for workspace {0}.".format(self.ws.slug))
            log.info("Resolving YouTube stream URLs for %s", self.video_id)
            self._streams = resolve_streams(self.url, self.settings)
            log.info(
                "  format %s, %sx%s @ %s fps",
                self._streams.format_ids,
                self._streams.width,
                self._streams.height,
                self._streams.fps,
            )
        return self._streams

    def tag(self) -> str:
        """Identifies what a cached segment was cut from: the video and the
        formats it resolved to. When HD finishes processing the best format
        changes, so segments cut earlier at lower quality are redone."""
        return "yt:{0}:{1}".format(self.video_id, self.get().format_ids)


def _input_args(url: str, headers: Dict[str, str], size: int, start: float):
    args = [
        "-request_size", str(size), "-multiple_requests", "1",
        # Without these a dropped connection ends the input early and ffmpeg
        # still exits 0 (see the module docstring); with them it reconnects, and
        # a stalled read gives up after READ_TIMEOUT_US and reconnects too.
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", str(RECONNECT_DELAY_MAX),
        "-rw_timeout", str(READ_TIMEOUT_US),
    ]
    agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
    if agent:
        args += ["-user_agent", agent]
    extra = "".join(
        "{0}: {1}\r\n".format(k, v) for k, v in headers.items() if k.lower() != "user-agent"
    )
    if extra:
        args += ["-headers", extra]
    return args + ["-ss", format_timestamp(start), "-i", url]


def segment_argv(
    binary: str,
    streams: Streams,
    start: float,
    end: float,
    out_path: Path,
    encoder: str,
    preset: str,
    crf: int,
    size: int = DEFAULT_REQUEST_SIZE,
    audio_bitrate: str = "160k",
):
    """ffmpeg argv for one re-encoded segment cut straight from the stream URLs."""
    argv = [
        binary, "-hide_banner", "-nostats", "-loglevel", "error",
        "-progress", "pipe:1", "-y",
    ]
    argv += _input_args(streams.video_url, streams.headers, size, start)
    if streams.audio_url:
        argv += _input_args(streams.audio_url, streams.headers, size, start)
        argv += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        argv += ["-map", "0:v:0", "-map", "0:a:0"]
    argv += [
        "-t", "{0:.3f}".format(end - start),
        "-c:v", encoder, "-preset", preset, "-crf", str(crf),
        # 10-bit VP9/AV1 sources would otherwise come out as High10 H.264,
        # which browsers and Instagram won't play.
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        str(out_path),
    ]
    return argv


def _looks_rejected(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _REJECTED_MARKERS)


def _explain(text: str) -> str:
    low = (text or "").lower()
    if "request_size" in low and ("unrecognized" in low or "not found" in low):
        return (
            "This ffmpeg is too old: cutting straight from YouTube needs the "
            "-request_size HTTP option (ffmpeg 8.1 or newer). Set tools.ffmpeg to a "
            "current build."
        )
    return ""


def probe_output(path: Path, settings: Settings) -> Optional[Dict[str, Optional[float]]]:
    """The tracks in a fetched segment: `{"video": seconds, "audio": seconds}`,
    a key per track that exists (the value None when ffprobe can't tell its
    length), or None when the file can't be read at all.

    Per track, never the container's duration: that is the longest track, so a
    file whose video stopped after 1.4 s while its audio ran on for 10 s still
    reports 10 s.
    """
    ffprobe = resolve_tool(settings.tool("ffprobe"), FFMPEG_HINT)
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "json", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    tracks = {}  # type: Dict[str, Optional[float]]
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind in ("video", "audio") and kind not in tracks:
            try:
                tracks[kind] = float(stream["duration"])
            except (KeyError, TypeError, ValueError):
                tracks[kind] = None
    return tracks


def segment_problem(
    path: Path, start: float, end: float, streams: Streams, settings: Settings
) -> str:
    """Why `path` isn't the complete segment `[start, end]`, or "" if it is.

    ffmpeg exits 0 after a connection drops mid-read and writes whatever it had
    (see the module docstring), so success from ffmpeg proves nothing: look at
    what came out. Each track must exist and reach roughly as far as the source
    allows - `dur=` in the stream URL says how far that is, so a clip running
    off the very end of the stream isn't taken for a truncated one.
    """
    tracks = probe_output(path, settings)
    if tracks is None:
        return "the file could not be read"
    for kind, source_length in (
        ("video", streams.video_duration),
        ("audio", streams.audio_duration),
    ):
        if kind not in tracks:
            return "it has no {0} track".format(kind)
        want = (min(end, source_length) if source_length else end) - start
        got = tracks[kind]
        if got is None or want <= 0:
            continue
        # Encoders land within a frame or two of the request; allow 5 % (at
        # least half a second) but never more than half of a very short clip.
        slack = min(max(0.5, 0.05 * want), 0.5 * want)
        if got < want - slack:
            return "its {0} track is {1:.1f}s long, expected about {2:.1f}s".format(
                kind, got, want
            )
    return ""


def fetch_segment(
    resolver: StreamResolver,
    start: float,
    end: float,
    out_path: Path,
    settings: Settings,
    encoder: str,
    preset: str,
    crf: int,
    progress: Progress = NULL_PROGRESS,
    base: float = 0.0,
    span: float = 1.0,
    log_path: Optional[Path] = None,
    label: Optional[str] = None,
) -> None:
    """Cut `[start, end]` (seconds on the video's own clock) out of the YouTube
    video into `out_path`, re-encoded. Raises StageError on failure, leaving no
    partial file behind - including when the result comes back incomplete twice
    in a row; `JobCancelled` propagates untouched (the caller cleans up, as
    compile/cut already do for local cuts)."""
    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    size = request_size(settings)
    problem = ""
    for attempt in (1, 2):
        streams = resolver.get(refresh=(attempt == 2))
        argv = segment_argv(binary, streams, start, end, out_path, encoder, preset, crf, size)
        try:
            run_ffmpeg(argv, end - start, progress, base, span, log_path, out_path, label=label)
        except StageError as exc:
            if out_path.exists():
                out_path.unlink()
            if attempt == 1 and _looks_rejected(str(exc)):
                log.info("YouTube rejected the stream URL (probably expired); resolving again")
                continue
            hint = _explain(str(exc))
            raise StageError("{0}\n\n{1}".format(exc, hint) if hint else str(exc))
        problem = segment_problem(out_path, start, end, streams, settings)
        if not problem:
            return
        if out_path.exists():
            out_path.unlink()
        if attempt == 1:
            log.warning(
                "Segment %s - %s came back incomplete (%s); fetching it again",
                format_timestamp(start),
                format_timestamp(end),
                problem,
            )
    raise StageError(
        "YouTube gave back an incomplete segment ({0}) twice in a row, so nothing was "
        "written. This is usually a network hiccup - try again in a minute.".format(problem)
    )


def ffmpeg_supports_request_size(settings: Settings) -> Optional[bool]:
    """Whether the configured ffmpeg has the -request_size HTTP option this
    module depends on. None if ffmpeg can't be run at all."""
    try:
        binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
        proc = subprocess.run(
            [binary, "-hide_banner", "-h", "protocol=https"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except Exception:  # noqa: BLE001 - a diagnostic must never raise
        return None
    return "request_size" in (proc.stdout or "")
