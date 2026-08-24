"""Stage 1: download a Kick VOD.

Primary path is yt-dlp. Kick changes its site often enough that yt-dlp
periodically breaks on it, so a kick-dl fallback is attempted when yt-dlp fails
and `download.fallback_to_kick_dl` is enabled.

Both downloaders write into the workspace root under a fixed basename, so the
resulting file is located by scanning rather than by parsing tool output (which
is the part most likely to change between releases).
"""

import collections
import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import (
    StageError,
    ToolMissingError,
    get_logger,
    human_size,
    resolve_tool,
    run_command,
)
from ..workspace import Workspace

log = get_logger(__name__)

STAGE = "download"

YT_DLP_HINT = (
    'Install with: pip install -U "yt-dlp[default,curl-cffi]" '
    "(the curl-cffi extra is required - Kick 403s without browser impersonation)"
)
# kick-dl is a Node CLI, not a Python package. The PyPI package of the same name
# is an unrelated, stale yt-dlp wrapper that pins yt-dlp backwards - don't use it.
KICK_DL_HINT = "Install with: npm install -g kick-dl"

# Quality presets exposed to callers (CLI --quality, dashboard quality picker),
# built with the same "prefer a bv*+ba combo over yt-dlp's bare 'best'" pattern
# already used by config/settings.json's default `download.format` - that
# combo, not the height cap itself, is what fixed the run that silently landed
# on 160p. "best" here means "no height cap", not "yt-dlp's literal best".
QUALITY_PRESETS = {
    "360": "bv*[height<=360]+ba/b[height<=360]/best",
    "480": "bv*[height<=480]+ba/b[height<=480]/best",
    "720": "bv*[height<=720]+ba/b[height<=720]/best",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/best",
    "best": "bv*+ba/b/best",
}
# Highest-to-lowest, for populating a dropdown.
QUALITY_CHOICES = ("best", "1080", "720", "480", "360")


def resolve_format(settings: Settings, quality: Optional[str] = None) -> str:
    """Turn a quality key into a yt-dlp `-f` selector.

    `quality=None` (the default everywhere) falls back to `download.format` -
    the existing hand-authored selector in config/settings.json, so nothing
    that doesn't explicitly ask for a quality changes behavior.
    """
    if not quality:
        return str(settings.get("download.format", "best"))
    try:
        return QUALITY_PRESETS[str(quality)]
    except KeyError:
        raise StageError(
            "Unknown quality '{0}' - choose one of: {1}".format(
                quality, ", ".join(QUALITY_CHOICES)
            )
        )


def _info_json_path(ws: Workspace) -> Path:
    return ws.root / "video.info.json"


def _read_metadata(ws: Workspace) -> dict:
    """Pull title/duration/uploader out of yt-dlp's --write-info-json output."""
    path = _info_json_path(ws)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            info = json.load(fh)
    except (ValueError, OSError) as exc:
        log.debug("Could not parse %s: %s", path, exc)
        return {}
    return {
        "title": info.get("title"),
        # Kick's own figure, which disagrees with the file: it reported 16523s
        # for a VOD that probes at 16492s. Deliberately NOT written as
        # `duration` - the audio stage probes the real thing and owns that key,
        # and this used to clobber it whenever download was re-run afterwards.
        "kick_duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "upload_date": info.get("upload_date"),
        # The wall-clock anchor the chat stage maps messages onto: unix epoch of
        # the stream's start. Chat messages carry absolute timestamps, so
        # `offset_into_vod = message_epoch - stream_started_at`. Note this is
        # when Kick created the livestream record, not necessarily when the
        # first recorded segment landed, hence chat.offset_seconds exists to
        # correct the residual.
        "stream_started_at": info.get("timestamp"),
        # Numeric channel id, needed by the chat API. Not the same as
        # `uploader_id` (that's the user id) - only this one returns messages.
        "kick_channel_id": info.get("channel_id"),
        # Recorded because the format selector has proven unreliable on Kick -
        # a run asking for the best format came back with 160p. This is what
        # actually landed on disk.
        "video_width": info.get("width"),
        "video_height": info.get("height"),
        "video_fps": info.get("fps"),
        "format_id": info.get("format_id"),
    }


def list_channel_vods(channel: str, limit: int = 20):
    """Ask Kick which VODs a channel actually has.

    Backs two callers: `_explain_404` (turning a bare 404 into something
    actionable - Kick's newer livestream session IDs, UUIDv7, e.g.
    019fc724-..., look like VOD IDs but aren't) and the dashboard's
    "browse channel" VOD picker (`GET /api/kick/{channel}/vods`), which is why
    this returns full metadata dicts rather than bare uuids.

    Response shape measured directly off `kick.com/api/v2/channels/<channel>/
    videos`: `duration` is milliseconds (not seconds), and the VOD's own id
    lives at `video.uuid`, not the top-level `id` (that's the livestream id).
    """
    try:
        from curl_cffi import requests
    except ImportError:
        return []
    try:
        response = requests.get(
            "https://kick.com/api/v2/channels/{0}/videos".format(channel),
            impersonate=str(settings_impersonate()),
            timeout=20,
        )
        if response.status_code != 200:
            return []
        out = []
        for item in response.json()[:limit]:
            video = item.get("video") or {}
            uuid = video.get("uuid") or item.get("uuid")
            if not uuid:
                continue
            duration_ms = item.get("duration")
            out.append(
                {
                    "uuid": uuid,
                    "title": item.get("session_title") or "",
                    "url": "https://kick.com/{0}/videos/{1}".format(channel, uuid),
                    "created_at": item.get("created_at"),
                    "duration_seconds": (
                        duration_ms / 1000.0
                        if isinstance(duration_ms, (int, float))
                        else None
                    ),
                    "thumbnail": (item.get("thumbnail") or {}).get("src"),
                    "is_live": bool(item.get("is_live")),
                    "viewer_count": item.get("viewer_count"),
                }
            )
        return out
    except Exception as exc:
        log.debug("Could not list VODs for %s: %s", channel, exc)
        return []


def settings_impersonate():
    return "chrome"


def _explain_404(url: str) -> str:
    """Build a helpful message when Kick says the VOD doesn't exist."""
    match = re.search(r"kick\.com/([^/]+)/videos?/", url)
    if not match:
        return ""
    channel = match.group(1)
    vods = list_channel_vods(channel, limit=5)
    if not vods:
        return ""
    lines = [
        "",
        "Kick has no VOD with that id. Note that a livestream's id (the newer "
        "UUIDv7 form, starting 019...) is not the same as its VOD id, so an id "
        "copied from the address bar while the stream was live will 404.",
        "",
        "Most recent VODs on /{0}:".format(channel),
    ]
    for vod in vods:
        lines.append("  {0}".format(vod["url"]))
    return "\n".join(lines)


def _hint_for(url: str, error) -> str:
    """Turn a download failure into something the reader can act on."""
    text = str(error or "")
    if "404" in text or "Not Found" in text:
        explained = _explain_404(url)
        if explained:
            return explained
    return (
        "\nIf Kick changed its site, try "
        "`pip install -U \"yt-dlp[default,curl-cffi]\"` first."
    )


# A private prefix on our --progress-template output, so the parser below
# can pick our machine-readable lines out of yt-dlp's normal stdout
# (destination/merge/postprocessor lines) by a simple startswith check
# rather than a regex over yt-dlp's human-readable bar (which varies in
# spacing/units between versions) - same reasoning ffrun.py gives for keying
# off ffmpeg's `-progress pipe:1` machine output instead of its normal log.
_PROGRESS_MARKER = "CLIPBOT_PROGRESS "
_PROGRESS_TEMPLATE = (
    "download:" + _PROGRESS_MARKER +
    "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s"
)


def _parse_progress_line(line: str):
    """Parse one `_PROGRESS_MARKER`-prefixed line into (downloaded, total,
    speed) floats. Any field yt-dlp couldn't determine yet comes through as
    the literal string "NA" and becomes None. Returns None if the line
    doesn't have the expected field count (defensive against a future
    yt-dlp template-field change)."""
    parts = line[len(_PROGRESS_MARKER):].split("|")
    if len(parts) != 4:
        return None

    def _f(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    downloaded, total, total_estimate, speed = (_f(p) for p in parts)
    return downloaded, (total or total_estimate), speed


def _run_yt_dlp(argv, progress: Progress, log_path: Optional[Path] = None) -> None:
    """Run yt-dlp, parsing `--progress-template` output into `progress`
    updates and honoring cancellation.

    Mirrors `ffrun.run_ffmpeg`'s shape (the established pattern in this
    codebase for a long subprocess with parseable progress and a cancel
    button) rather than `utils.run_command`, which has neither.
    """
    proc = subprocess.Popen(
        [str(a) for a in argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail = collections.deque(maxlen=80)
    last_downloaded = 0.0
    phase_open = False
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # Matches run_command(tee=True)'s old behavior of debug-logging
            # every raw line - visible under `-v`, quiet by default, same as
            # every other long-running stage in this codebase (transcribe,
            # analyze, reel, compile all stay console-quiet during the work
            # itself and rely on a summary log line at the end).
            log.debug("%s", line)
            if progress.cancelled:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise JobCancelled("cancelled by request")

            if line.startswith(_PROGRESS_MARKER):
                parsed = _parse_progress_line(line)
                if parsed is None or parsed[0] is None:
                    continue
                downloaded, total, speed = parsed
                if not phase_open or downloaded < last_downloaded:
                    # `-f bv*+ba/b/best` downloads video then audio as two
                    # separate files; yt-dlp's own downloaded_bytes resets
                    # when the second one starts. A fresh phase here mirrors
                    # yt-dlp's own two-pass terminal output (video bar, then
                    # audio bar) - not a bug.
                    progress.phase("download", unit="bytes")
                    phase_open = True
                last_downloaded = downloaded
                progress.update(downloaded, total=total, rate=speed)
            else:
                tail.append(line)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "\n".join([" ".join(str(a) for a in argv), ""] + list(tail)),
                encoding="utf-8",
            )
        except OSError:
            pass

    if proc.returncode != 0:
        raise StageError(
            "yt-dlp failed (exit {0}):\n{1}".format(
                proc.returncode, "\n".join(tail)[-4000:] or "(no output)"
            )
        )


def _download_with_yt_dlp(
    url: str,
    ws: Workspace,
    settings: Settings,
    quality: Optional[str] = None,
    progress: Progress = NULL_PROGRESS,
) -> None:
    binary = resolve_tool(settings.tool("yt_dlp"), YT_DLP_HINT)
    argv = [
        binary,
        "--no-playlist",
        "--newline",
        "--write-info-json",
        "-f",
        resolve_format(settings, quality),
        "--progress-template",
        _PROGRESS_TEMPLATE,
        "-o",
        str(ws.root / "video.%(ext)s"),
    ]

    # Kick sits behind Cloudflare and returns 403 to yt-dlp's normal HTTP client.
    # Impersonating a real browser's TLS fingerprint is what makes it work; it
    # needs the curl-cffi extra (pip install "yt-dlp[default,curl-cffi]").
    impersonate = settings.get("download.impersonate", "chrome")
    if impersonate:
        argv += ["--impersonate", str(impersonate)]

    # yt-dlp's own documented flag for "bypassing bandwidth throttling
    # imposed by a webserver" - see the setting's _comment in settings.json
    # for what's actually been measured/ruled out on this network, and why
    # this alone isn't a guaranteed fix (sequential chunking, not parallel
    # connections, in the installed yt-dlp version).
    chunk_size = settings.get("download.http_chunk_size")
    if chunk_size:
        argv += ["--http-chunk-size", str(chunk_size)]

    argv.append(url)
    log.info("Downloading with yt-dlp: %s", url)
    _run_yt_dlp(argv, progress, log_path=ws.logs_dir / "download.log")


def _download_with_kick_dl(url: str, ws: Workspace, settings: Settings) -> None:
    """Fallback downloader - currently non-functional, and off by default.

    kick-dl 2.0.0 ships a single command, `kick-dl start`, which takes no
    arguments and drives an interactive TUI. There is no scriptable download
    path, so this cannot serve as an automated fallback: from the CLI it just
    fails with "unknown option", and from a dashboard worker thread it would
    block forever waiting on a prompt nobody can answer.

    Kept wired so it's ready if a future release adds non-interactive flags;
    enable again via download.fallback_to_kick_dl.
    """
    binary = resolve_tool(settings.tool("kick_dl"), KICK_DL_HINT)
    argv = [binary, "download", url, "--output", str(ws.root)]
    log.info("Retrying download with kick-dl: %s", url)
    run_command(argv, log=log, capture=True)


def download_vod(
    url: str,
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    quality: Optional[str] = None,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Download `url` into the workspace. Returns the video file path.

    `quality` is a key into QUALITY_PRESETS ("best"/"1080"/"720"/"480"/"360");
    None keeps the existing `download.format` setting untouched. Only the
    yt-dlp path honors it or `progress` - kick-dl's fallback has no
    scriptable quality selection or parseable progress output to plumb them
    into (see `_download_with_kick_dl`'s docstring).
    """
    existing = ws.video_path()
    if existing and not force:
        log.info(
            "Video already present, skipping download: %s (%s)",
            existing.name,
            human_size(existing.stat().st_size),
        )
        return existing

    if existing and force:
        log.info("--force: removing existing %s", existing.name)
        existing.unlink()

    primary_error: Optional[Exception] = None
    try:
        _download_with_yt_dlp(url, ws, settings, quality, progress)
    except (StageError, ToolMissingError) as exc:
        primary_error = exc
        if not settings.get("download.fallback_to_kick_dl", True):
            # No fallback configured, so this is the final error - attach the
            # explanation here rather than only on the both-failed path.
            hint = _hint_for(url, exc)
            raise StageError("{0}\n{1}".format(exc, hint)) if hint else exc
        log.warning("yt-dlp failed: %s", exc)
        try:
            _download_with_kick_dl(url, ws, settings)
        except (StageError, ToolMissingError) as fallback_exc:
            hint = _hint_for(url, primary_error)
            raise StageError(
                "Both downloaders failed.\n"
                "  yt-dlp:  {0}\n"
                "  kick-dl: {1}\n{2}".format(primary_error, fallback_exc, hint)
            )

    video = ws.video_path()
    if not video:
        raise StageError(
            "Download reported success but no video file was found in {0}".format(
                ws.root
            )
        )

    metadata = _read_metadata(ws)
    size = video.stat().st_size
    height = metadata.get("video_height")
    log.info(
        "Downloaded %s (%s%s)",
        video.name,
        human_size(size),
        ", {0}x{1}".format(metadata.get("video_width"), height) if height else "",
    )

    min_height = settings.get("download.min_height_warn", 480)
    if height and min_height and int(height) < int(min_height):
        log.warning(
            "This VOD came back at %sx%s (format %s) - too low to review clips "
            "visually. Kick offers up to 720p60; re-run with --force to try again.",
            metadata.get("video_width"),
            height,
            metadata.get("format_id"),
        )

    fields = {k: v for k, v in metadata.items() if v is not None}
    # Seed `duration` so the dashboard has something to show between download
    # and audio extraction, but never overwrite the probed value - Kick's figure
    # is ~31s long on a 4.6h VOD, and every clip range is clamped against this.
    if fields.get("kick_duration") and not ws.read_state().get("duration"):
        fields["duration"] = fields["kick_duration"]

    ws.update_state(
        url=url,
        video_file=video.name,
        video_bytes=size,
        **fields
    )
    ws.mark_stage(STAGE, video_file=video.name, bytes=size)
    return video


def delete_vod(ws: Workspace) -> bool:
    """Stage 6's cleanup: drop the full VOD, keep transcript + clips."""
    video = ws.video_path()
    if not video or not video.exists():
        log.info("No VOD file to delete in %s", ws.root)
        return False
    size = video.stat().st_size
    video.unlink()
    log.info("Deleted %s, reclaimed %s", video.name, human_size(size))
    ws.update_state(video_file=None, video_deleted=True)
    return True
