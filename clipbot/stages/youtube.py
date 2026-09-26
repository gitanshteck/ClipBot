"""YouTube ingest: metadata, audio-only download, channel listing, health checks.

A YouTube workspace never needs the video on disk to find clips: transcription
and analysis only read `audio.wav`, and the dashboard plays the embedded
YouTube video. So the "download" stage here fetches **audio only** (~0.27 GB
for a 4.6 h stream instead of ~15 GB of 1080p), and the existing
`audio.extract_audio(video=<that file>)` turns it into the usual 16 kHz mono
`audio.wav` - which then deletes the source, since audio.wav is the artifact
everything downstream reads.

Same ground rules as Kick's stage 1, different tool flags:

* No `--impersonate` and no `--http-chunk-size`. Both are Kick workarounds
  (Cloudflare's TLS fingerprinting, a per-connection throttle); neither
  applies to YouTube.
* Progress and Cancel come from the shared runner in `clipbot/ytdlp.py`.
* `state["duration"]` stays owned by the audio probe. YouTube's own figure is
  recorded as `youtube_duration` and only seeds `duration` if it is absent, the
  same treatment Kick's `kick_duration` gets.

Current yt-dlp needs Python 3.10+ and an external JavaScript runtime (Deno 2.3+
by default, Node 22+ if `download.youtube.js_runtime` says so) to solve
YouTube's challenges; without both it fails at extraction. `health_checks()`
reports exactly that, and `explain_failure()` turns the common yt-dlp errors
into what to do about them.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..progress import NULL_PROGRESS, Progress
from ..utils import (
    StageError,
    ToolMissingError,
    get_logger,
    human_size,
    resolve_tool,
)
from ..workspace import SOURCE_AUDIO_STEM, Workspace
from ..ytdlp import PROGRESS_TEMPLATE, run_yt_dlp

log = get_logger(__name__)

STAGE = "download"

YT_DLP_HINT = (
    "YouTube needs a current yt-dlp (Python 3.10+) and a JavaScript runtime. "
    'Install with: pip install -U "yt-dlp[default,curl-cffi]" and '
    "winget install DenoLand.Deno, then point CLIPBOT_YT_DLP at the new yt-dlp."
)

DEFAULT_AUDIO_FORMAT = "bestaudio/best"
DEFAULT_PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

# yt-dlp's live_status values that mean "there is no complete archive yet".
LIVE_NOW = ("is_live", "is_upcoming")

# Runtime minimums from yt-dlp's EJS wiki page. Deno is the default runtime.
MIN_DENO = (2, 3, 0)
MIN_NODE = (22, 0, 0)

# yt-dlp ships roughly monthly and YouTube breaks old versions within weeks.
STALE_YT_DLP_DAYS = 90

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")


# --------------------------------------------------------------------------
# argv
# --------------------------------------------------------------------------


def _common_flags(settings: Settings, playlist: bool = False) -> List[str]:
    """Flags every YouTube yt-dlp call shares. All optional settings: an
    unconfigured install passes nothing beyond `--no-playlist`."""
    flags = [] if playlist else ["--no-playlist"]
    runtime = settings.get("download.youtube.js_runtime")
    if runtime:
        flags += ["--js-runtimes", str(runtime)]
    browser = settings.get("download.youtube.cookies_from_browser")
    if browser:
        flags += ["--cookies-from-browser", str(browser)]
    client = settings.get("download.youtube.player_client")
    if client:
        flags += ["--extractor-args", "youtube:player_client={0}".format(client)]
    return flags


def _binary(settings: Settings) -> str:
    return resolve_tool(settings.tool("yt_dlp"), YT_DLP_HINT)


def base_argv(settings: Settings, playlist: bool = False) -> List[str]:
    """`[yt-dlp, <the flags every YouTube call shares>]`: what other modules
    (clipbot/ytsegments.py) build their own yt-dlp calls on, so the configured
    binary, JS runtime, cookies and player client apply to them too."""
    return [_binary(settings)] + _common_flags(settings, playlist=playlist)


def info_json_path(ws: Workspace) -> Path:
    # Same name Kick's download writes, so everything that reads it (the
    # dashboard's title fallback) works for both platforms.
    return ws.root / "video.info.json"


def metadata_argv(url: str, ws: Workspace, settings: Settings) -> List[str]:
    return (
        [_binary(settings)]
        + _common_flags(settings)
        + [
            "--skip-download",
            "--write-info-json",
            "-o",
            str(ws.root / "video.%(ext)s"),
            url,
        ]
    )


def audio_argv(url: str, ws: Workspace, settings: Settings) -> List[str]:
    audio_format = settings.get("download.youtube.audio_format") or DEFAULT_AUDIO_FORMAT
    return (
        [_binary(settings)]
        + _common_flags(settings)
        + [
            "--newline",
            "-f",
            str(audio_format),
            "--progress-template",
            PROGRESS_TEMPLATE,
            "-o",
            str(ws.root / (SOURCE_AUDIO_STEM + ".%(ext)s")),
            url,
        ]
    )


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------


def explain_failure(text: str) -> str:
    """What to do about a yt-dlp failure, for the ones that have a known fix.
    Empty string when we don't recognise it (the raw error is still shown)."""
    low = (text or "").lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return (
            "YouTube is asking for proof you're not a bot. Set "
            "download.youtube.cookies_from_browser to a browser you're signed in "
            "to (Firefox avoids Chrome's locked cookie database)."
        )
    if (
        "page needs to be reloaded" in low
        or "sabr" in low
        or "javascript runtime" in low
        or "js runtime" in low
        or "challenge" in low
        or "requested format is not available" in low
    ):
        return (
            "yt-dlp couldn't get playable formats from YouTube. Update it (pip "
            "install -U \"yt-dlp[default,curl-cffi]\"; needs Python 3.10+) and make "
            "sure a JavaScript runtime is installed (Deno 2.3+: winget install "
            "DenoLand.Deno). Doctor shows what was found."
        )
    if "private video" in low or "members-only" in low or "join this channel" in low:
        return "This video is private or members-only, so it can't be fetched without your login."
    if "video unavailable" in low or "this video is not available" in low:
        return "YouTube says the video is unavailable (deleted, or not visible to this account)."
    return ""


def _run(argv, progress: Progress, log_name: str, ws: Workspace) -> None:
    try:
        run_yt_dlp(argv, progress, log_path=ws.logs_dir / log_name)
    except StageError as exc:
        hint = explain_failure(str(exc))
        if hint:
            raise StageError("{0}\n\n{1}".format(exc, hint))
        raise


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------


def metadata_from_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """The state.json fields worth keeping from yt-dlp's info.json."""
    return {
        "title": info.get("title"),
        "uploader": info.get("channel") or info.get("uploader"),
        "upload_date": info.get("upload_date"),
        # When the broadcast started, for a future chat-replay stage. Prefer
        # the scheduled/actual release time over the upload timestamp.
        "stream_started_at": info.get("release_timestamp") or info.get("timestamp"),
        # Informational only - the audio stage's probed `duration` is the one
        # every clip range is clamped against.
        "youtube_duration": info.get("duration"),
        "youtube_channel_id": info.get("channel_id"),
        "youtube_live_status": info.get("live_status"),
    }


def _read_info(ws: Workspace) -> Optional[Dict[str, Any]]:
    path = info_json_path(ws)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        log.debug("Could not parse %s: %s", path, exc)
        return None


def fetch_metadata(
    url: str,
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Dict[str, Any]:
    """Fetch (or reuse) `video.info.json` and record its fields in state.json.

    A cached info.json is reused only when it describes a finished video: one
    written while the stream was still live would otherwise pin
    `is_live` forever and block the audio fetch after the stream has ended.
    """
    info = None if force else _read_info(ws)
    if info is not None and info.get("live_status") in LIVE_NOW + ("post_live",):
        info = None

    if info is None:
        log.info("Fetching YouTube metadata: %s", url)
        _run(metadata_argv(url, ws, settings), progress, "youtube-metadata.log", ws)
        info = _read_info(ws)
        if info is None:
            raise StageError(
                "yt-dlp reported success but wrote no {0}".format(info_json_path(ws).name)
            )

    fields = {k: v for k, v in metadata_from_info(info).items() if v is not None}
    # Seed `duration` so the dashboard has a length to show before the audio
    # stage probes the real one - and never overwrite the probed value.
    if fields.get("youtube_duration") and not ws.read_state().get("duration"):
        fields["duration"] = fields["youtube_duration"]
    ws.update_state(**fields)
    return fields


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------


def fetch_audio(
    url: str,
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Download the video's audio track into the workspace.

    Returns the file `extract_audio(video=...)` should read. If audio.wav is
    already extracted from an earlier run (whose source has since been deleted)
    there is nothing to fetch: that is returned instead, and `extract_audio`'s
    own skip-if-present check makes it a no-op - which is only safe because
    `force` bypasses this early return, so a forced re-extract never reads
    audio.wav as its own input.
    """
    existing = ws.source_audio_path()
    if existing and not force:
        log.info(
            "Source audio already present, skipping download: %s (%s)",
            existing.name,
            human_size(existing.stat().st_size),
        )
        return existing
    if ws.audio_path.exists() and not force:
        log.info("audio.wav already extracted, nothing to fetch")
        return ws.audio_path

    fields = fetch_metadata(url, ws, settings, force=force, progress=progress)
    status = fields.get("youtube_live_status")
    if status in LIVE_NOW:
        raise StageError(
            "This YouTube video is {0}. Wait until the stream has ended and its "
            "archive is available, then try again.".format(
                "live right now" if status == "is_live" else "scheduled but hasn't started"
            )
        )
    if status == "post_live":
        log.warning(
            "YouTube is still processing this stream's archive; the audio may be "
            "incomplete. Re-run with force once it has finished."
        )

    if existing and force:
        log.info("--force: removing existing %s", existing.name)
        existing.unlink()

    log.info("Downloading audio only with yt-dlp: %s", url)
    _run(audio_argv(url, ws, settings), progress, "download.log", ws)

    audio = ws.source_audio_path()
    if not audio:
        raise StageError(
            "Download reported success but no audio file was found in {0}".format(ws.root)
        )
    size = audio.stat().st_size
    log.info("Downloaded %s (%s)", audio.name, human_size(size))
    ws.mark_stage(STAGE, audio_only=True, source_audio=audio.name, bytes=size)
    return audio


def drop_source_audio(ws: Workspace, source: Path, settings: Settings) -> bool:
    """Delete the audio-only download once audio.wav has been extracted from
    it. Only ever removes the workspace's own `source_audio.*`, never a file a
    caller passed in from elsewhere. Returns True if something was deleted."""
    if settings.get("download.youtube.keep_source_audio", False):
        return False
    own = ws.source_audio_path()
    if not own or Path(source).resolve() != own.resolve():
        return False
    size = own.stat().st_size
    try:
        own.unlink()
    except OSError as exc:
        log.warning("Could not delete %s: %s", own.name, exc)
        return False
    log.info("Deleted %s, reclaimed %s (audio.wav is the kept copy)", own.name, human_size(size))
    return True


# --------------------------------------------------------------------------
# channel listing
# --------------------------------------------------------------------------


def watch_url(video_id: str) -> str:
    return "https://www.youtube.com/watch?v={0}".format(video_id)


def _stream_from_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    video_id = entry.get("id")
    if not video_id:
        return None
    return {
        "video_id": video_id,
        "title": entry.get("title") or "",
        "url": watch_url(video_id),
        "duration_seconds": entry.get("duration"),
        # i.ytimg.com serves a 16:9 320x180 thumbnail for every public id, so
        # there is no need to depend on which thumbnails --flat-playlist returned.
        "thumbnail": "https://i.ytimg.com/vi/{0}/mqdefault.jpg".format(video_id),
        "live_status": entry.get("live_status"),
        "view_count": entry.get("view_count"),
    }


def list_channel_streams(
    handle: str, settings: Settings, limit: int = 20, timeout: int = 120
) -> List[Dict[str, Any]]:
    """A channel's past streams, newest first, via yt-dlp's flat listing of the
    channel's Streams tab (no API key, no quota). Raises StageError on failure."""
    handle = (handle or "").strip().lstrip("@")
    if not _HANDLE_RE.match(handle):
        raise StageError("Not a valid YouTube channel handle: {0!r}".format(handle))
    argv = (
        [_binary(settings)]
        + _common_flags(settings, playlist=True)
        + [
            "--flat-playlist",
            "--playlist-end",
            str(max(1, int(limit))),
            "-J",
            "https://www.youtube.com/@{0}/streams".format(handle),
        ]
    )
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise StageError("Listing @{0}'s streams timed out after {1}s.".format(handle, timeout))
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-1500:]
        hint = explain_failure(detail)
        raise StageError(
            "yt-dlp couldn't list @{0}'s streams:\n{1}{2}".format(
                handle, detail, "\n\n" + hint if hint else ""
            )
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise StageError("yt-dlp returned unreadable JSON for @{0}'s streams.".format(handle))
    streams = [_stream_from_entry(e) for e in (data.get("entries") or []) if isinstance(e, dict)]
    return [s for s in streams if s]


# --------------------------------------------------------------------------
# health (Doctor)
# --------------------------------------------------------------------------


def _parse_version(text: str) -> Optional[Tuple[int, ...]]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in match.groups()) if match else None


def _fmt_version(version: Tuple[int, ...]) -> str:
    return ".".join(str(p) for p in version)


def _tool_version(argv: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def yt_dlp_age_days(version: str, today: Optional[float] = None) -> Optional[int]:
    """Age of a yt-dlp release (they are versioned YYYY.MM.DD)."""
    match = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version or "")
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        released = time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return None
    return int(((today if today is not None else time.time()) - released) // 86400)


def _runtime_check(settings: Settings) -> Dict[str, Any]:
    configured = str(settings.get("download.youtube.js_runtime") or "deno")
    name = configured.split(":", 1)[0].strip().lower() or "deno"

    if name == "deno":
        found = shutil.which("deno")
        if not found:
            return {
                "name": "JS runtime (Deno)",
                "ok": False,
                "detail": "deno not found - yt-dlp needs it to solve YouTube's challenges. "
                "Install: winget install DenoLand.Deno (then restart the dashboard)",
            }
        version = _parse_version(_tool_version([found, "--version"]) or "")
        if version is None:
            return {"name": "JS runtime (Deno)", "ok": False, "detail": "found at {0} but its version couldn't be read".format(found)}
        ok = version >= MIN_DENO
        return {
            "name": "JS runtime (Deno)",
            "ok": ok,
            "detail": "{0} at {1}".format(_fmt_version(version), found)
            if ok
            else "{0} is too old - yt-dlp needs {1}+".format(_fmt_version(version), _fmt_version(MIN_DENO)),
        }

    if name == "node":
        found = shutil.which("node")
        if not found:
            return {"name": "JS runtime (Node)", "ok": False, "detail": "node not found on PATH"}
        version = _parse_version(_tool_version([found, "--version"]) or "")
        ok = bool(version and version >= MIN_NODE)
        return {
            "name": "JS runtime (Node)",
            "ok": ok,
            "detail": "{0} at {1}".format(_fmt_version(version), found)
            if ok
            else "{0} is too old - yt-dlp needs Node {1}+".format(
                _fmt_version(version) if version else "unknown version", _fmt_version(MIN_NODE)
            ),
        }

    found = shutil.which(name)
    return {
        "name": "JS runtime ({0})".format(name),
        "ok": bool(found),
        "detail": found or "{0} not found on PATH".format(name),
    }


def health_checks(settings: Settings) -> List[Dict[str, Any]]:
    """Cheap, offline checks for the Doctor dialog: is yt-dlp current, and is
    there a JS runtime it can use. No network - see `probe_youtube` for that."""
    checks: List[Dict[str, Any]] = []
    try:
        binary = _binary(settings)
    except ToolMissingError:
        return [{"name": "yt-dlp (YouTube)", "ok": False, "detail": YT_DLP_HINT}]

    raw = _tool_version([binary, "--version"])
    version = (raw or "").strip().splitlines()[-1] if raw and raw.strip() else ""
    if not version:
        checks.append({"name": "yt-dlp version", "ok": False, "detail": "couldn't run {0} --version".format(binary)})
    else:
        age = yt_dlp_age_days(version)
        stale = age is not None and age > STALE_YT_DLP_DAYS
        detail = "{0} ({1} days old)".format(version, age) if age is not None else version
        if stale:
            detail += " - YouTube changes often; update: pip install -U \"yt-dlp[default,curl-cffi]\" (Python 3.10+)"
        checks.append({"name": "yt-dlp version", "ok": not stale, "detail": detail})

    checks.append(_runtime_check(settings))
    return checks


def probe_youtube(settings: Settings, timeout: int = 90) -> Dict[str, Any]:
    """Actually ask YouTube for a public video's format list. This is the only
    check that proves extraction works end to end; it costs a few seconds and a
    network round trip, so the Doctor runs it on request rather than on open."""
    url = str(settings.get("download.youtube.probe_url") or DEFAULT_PROBE_URL)
    try:
        binary = _binary(settings)
    except ToolMissingError:
        return {"name": "YouTube extraction", "ok": False, "detail": YT_DLP_HINT}
    argv = [binary] + _common_flags(settings) + ["-F", "--no-warnings", url]
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"name": "YouTube extraction", "ok": False, "detail": "timed out after {0}s".format(timeout)}
    except OSError as exc:
        return {"name": "YouTube extraction", "ok": False, "detail": str(exc)}

    took = time.time() - started
    output = proc.stdout or ""
    if proc.returncode != 0:
        lines = [l for l in output.strip().splitlines() if l.strip()]
        detail = lines[-1] if lines else "yt-dlp exited {0}".format(proc.returncode)
        hint = explain_failure(output)
        return {
            "name": "YouTube extraction",
            "ok": False,
            "detail": detail + (" - " + hint if hint else ""),
        }
    audio_only = "audio only" in output
    return {
        "name": "YouTube extraction",
        "ok": audio_only,
        "detail": "listed formats in {0:.1f}s{1}".format(
            took, ", audio-only available" if audio_only else " but none were audio-only"
        ),
    }
