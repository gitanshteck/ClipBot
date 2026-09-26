"""Stage 2: extract the audio track from the downloaded VOD.

Runs immediately after download so the transcription stage no longer depends on
the video file being present. Output is 16 kHz mono PCM, which is what Whisper
resamples to internally anyway.
"""

import json
from pathlib import Path
from typing import Optional

from .. import platforms
from ..config import Settings
from ..utils import (
    StageError,
    get_logger,
    human_size,
    resolve_tool,
    run_command,
)
from ..workspace import Workspace
from . import youtube as youtube_stage

log = get_logger(__name__)

STAGE = "audio"

FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add "
    "the bin folder to PATH or set tools.ffmpeg / tools.ffprobe in "
    "config/settings.json to the full ffmpeg.exe / ffprobe.exe paths."
)


def probe_duration(media: Path, settings: Settings) -> Optional[float]:
    """Media duration in seconds via ffprobe, or None if it can't be read."""
    try:
        binary = resolve_tool(settings.tool("ffprobe"), FFMPEG_HINT)
    except Exception as exc:  # ffprobe is optional - duration is informational
        log.debug("ffprobe unavailable: %s", exc)
        return None

    result = run_command(
        [
            binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(media),
        ],
        log=log,
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        log.debug("ffprobe failed on %s: %s", media.name, (result.stderr or "").strip())
        return None
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        log.debug("Could not parse ffprobe output: %s", exc)
        return None


def trim_audio(
    source: Path,
    out_path: Path,
    seconds: float,
    settings: Settings,
    start: float = 0.0,
) -> Path:
    """Copy `seconds` of `source` starting at `start` to `out_path`.

    Used for benchmarking. A stream's opening minutes are usually a waiting
    screen with music, so benchmark a mid-stream slice for a fair read.
    """
    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    run_command(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start),
            "-t",
            str(seconds),
            "-i",
            str(source),
            "-c",
            "copy",
            str(out_path),
        ],
        log=log,
    )
    log.info(
        "Trimmed %.0fs from %.0fs of %s -> %s",
        seconds,
        start,
        source.name,
        out_path.name,
    )
    return out_path


def extract_audio(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    video: Optional[Path] = None,
) -> Path:
    """Extract audio from the workspace's VOD. Returns the audio file path.

    The source is, in order: an explicit `video`, the workspace's YouTube
    audio-only download (`source_audio.*`, deleted again once audio.wav
    exists - see stages/youtube.py), then the downloaded VOD. A Kick workspace
    has no `source_audio.*`, so for it this resolves exactly as it always did.
    """
    out_path = ws.audio_path
    if out_path.exists() and not force:
        log.info(
            "Audio already extracted, skipping: %s (%s)",
            out_path.name,
            human_size(out_path.stat().st_size),
        )
        return out_path

    source = video or ws.source_audio_path() or ws.video_path()
    if not source or not source.exists():
        if ws.platform == platforms.YOUTUBE:
            raise StageError(
                "No source audio in {0}. Run the 'Fetch audio' (download) stage "
                "first - the audio download is deleted once audio.wav has been "
                "extracted from it, so re-extracting means fetching it again.".format(ws.root)
            )
        raise StageError(
            "No video file in {0}. Run the download stage first "
            "(the VOD may have been deleted by the cleanup stage).".format(ws.root)
        )

    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    argv = [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stats",
        "-y",
        "-i",
        str(source),
        "-vn",  # drop video
        "-sn",  # drop subtitles
        "-dn",  # drop data streams
        "-ac",
        str(settings.get("audio.channels", 1)),
        "-ar",
        str(settings.get("audio.sample_rate", 16000)),
        "-c:a",
        str(settings.get("audio.codec", "pcm_s16le")),
        str(out_path),
    ]

    log.info("Extracting audio from %s", source.name)
    run_command(argv, log=log)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise StageError("ffmpeg produced no audio at {0}".format(out_path))

    duration = probe_duration(out_path, settings)
    size = out_path.stat().st_size
    log.info(
        "Wrote %s (%s%s)",
        out_path.name,
        human_size(size),
        ", {0:.1f} min".format(duration / 60.0) if duration else "",
    )

    ws.update_state(
        audio_file=out_path.name,
        audio_bytes=size,
        **({"duration": duration} if duration else {})
    )
    ws.mark_stage(STAGE, audio_file=out_path.name, bytes=size, duration=duration)
    youtube_stage.drop_source_audio(ws, source, settings)
    return out_path
