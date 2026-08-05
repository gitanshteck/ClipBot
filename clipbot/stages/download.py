"""Stage 1: download a Kick VOD.

Primary path is yt-dlp. Kick changes its site often enough that yt-dlp
periodically breaks on it, so a kick-dl fallback is attempted when yt-dlp fails
and `download.fallback_to_kick_dl` is enabled.

Both downloaders write into the workspace root under a fixed basename, so the
resulting file is located by scanning rather than by parsing tool output (which
is the part most likely to change between releases).
"""

import json
import re
from pathlib import Path
from typing import Optional

from ..config import Settings
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


def list_channel_vods(channel: str, limit: int = 5):
    """Ask Kick which VODs a channel actually has.

    Used to turn a bare 404 into something actionable: Kick's newer livestream
    session IDs (UUIDv7, e.g. 019fc724-...) look like VOD IDs but aren't, and
    copying one out of the address bar mid-stream is an easy mistake.
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
            uuid = (item.get("video") or {}).get("uuid") or item.get("uuid")
            if uuid:
                out.append(uuid)
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
    vods = list_channel_vods(channel)
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
    for uuid in vods:
        lines.append("  https://kick.com/{0}/videos/{1}".format(channel, uuid))
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


def _download_with_yt_dlp(url: str, ws: Workspace, settings: Settings) -> None:
    binary = resolve_tool(settings.tool("yt_dlp"), YT_DLP_HINT)
    argv = [
        binary,
        "--no-playlist",
        "--newline",
        "--write-info-json",
        "-f",
        str(settings.get("download.format", "best")),
        "-o",
        str(ws.root / "video.%(ext)s"),
    ]

    # Kick sits behind Cloudflare and returns 403 to yt-dlp's normal HTTP client.
    # Impersonating a real browser's TLS fingerprint is what makes it work; it
    # needs the curl-cffi extra (pip install "yt-dlp[default,curl-cffi]").
    impersonate = settings.get("download.impersonate", "chrome")
    if impersonate:
        argv += ["--impersonate", str(impersonate)]

    argv.append(url)
    log.info("Downloading with yt-dlp: %s", url)
    # tee=True keeps progress on the console while still capturing the tail, so
    # a failure carries yt-dlp's actual error rather than just an exit code.
    run_command(argv, log=log, capture=True, tee=True)


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
) -> Path:
    """Download `url` into the workspace. Returns the video file path."""
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
        _download_with_yt_dlp(url, ws, settings)
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
