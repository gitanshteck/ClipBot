"""Stage 5: cut the flagged ranges out of the downloaded video.

Reads review state from `clips.json` when it exists (so the dashboard's
approve/reject decisions are honoured) and falls back to cutting everything in
`candidates.json` so the CLI works without ever opening the dashboard.

Two things here are easy to get wrong and expensive if you do:

* `-ss` goes **before** `-i`. With `-c copy` and an output seek, ffmpeg decodes
  and discards everything before the cut point - on a 73-minute VOD that is the
  difference between two seconds and two minutes per clip.
* Stream copy snaps the start to the nearest keyframe, so a copy-mode cut can
  begin up to a few seconds earlier than asked. That is the right default
  (it's instant and lossless); `cut.re_encode` trades speed for frame accuracy.

A YouTube workspace has no video on disk, so its clips are cut straight from
YouTube instead (clipbot/ytsegments.py): only the requested seconds are fetched,
and they are always re-encoded - a stream-copy cut from two separate remote
inputs has no clean start (see that module) - using the same `cut.encoder` /
`cut.preset` / `cut.crf` a re-encoding Kick cut would.
"""

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import review
from .. import ytsegments
from ..config import Settings
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import (
    StageError,
    format_timestamp,
    get_logger,
    human_size,
    resolve_tool,
    run_command,
    slugify,
)
from ..workspace import MODE_EMBED, Workspace

log = get_logger(__name__)

STAGE = "cut"

FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add "
    "the bin folder to PATH or set tools.ffmpeg in config/settings.json."
)


def _fingerprint(
    start: float, end: float, settings: Settings, re_encode: bool, source_tag: str = ""
) -> str:
    """Identifies the exact cut that produced a file.

    Changing the in/out points or the padding changes this, so an edited clip is
    re-cut automatically without needing --force. `source_tag` (YouTube only:
    the video and the formats it resolved to) is appended only when given, so a
    local cut's fingerprint is byte-identical to what it always was.
    """
    fingerprint = "{0:.3f}|{1:.3f}|{2}|{3}|{4}".format(
        start,
        end,
        settings.get("cut.pad_start", 1.0),
        settings.get("cut.pad_end", 1.5),
        "encode" if re_encode else "copy",
    )
    if source_tag:
        fingerprint += "|" + source_tag
    return fingerprint


def _clip_label(clip: Dict[str, Any], index: int) -> str:
    raw = clip.get("title") or clip.get("description") or clip.get("id") or "clip"
    return "{0:03d}-{1}".format(index, slugify(raw, max_length=40) or "clip")


def _padded_range(clip: Dict[str, Any], settings: Settings, duration: Optional[float]):
    pad_start = float(settings.get("cut.pad_start", 1.0))
    pad_end = float(settings.get("cut.pad_end", 1.5))
    start = max(0.0, float(clip["start"]) - pad_start)
    end = float(clip["end"]) + pad_end
    if duration:
        end = min(end, float(duration))
    return start, end


def _build_argv(
    binary: str,
    source: Path,
    out_path: Path,
    start: float,
    end: float,
    settings: Settings,
    re_encode: bool,
) -> List[str]:
    argv = [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        # Input seek: fast, and the only sane option in copy mode.
        "-ss",
        format_timestamp(start),
        "-i",
        str(source),
        "-t",
        "{0:.3f}".format(end - start),
    ]
    if re_encode:
        argv += [
            "-c:v",
            str(settings.get("cut.encoder", "libx264")),
            "-preset",
            str(settings.get("cut.preset", "veryfast")),
            "-crf",
            str(settings.get("cut.crf", 20)),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
        ]
    else:
        argv += ["-c", "copy", "-avoid_negative_ts", "make_zero"]

    # +faststart puts the index at the front so the browser can seek the clip
    # immediately and uploads to Instagram/YouTube behave.
    argv += ["-movflags", "+faststart", str(out_path)]
    return argv


def _load_clips(ws: Workspace, clip_ids=None):
    """Prefer review state; fall back to raw candidates for CLI-only use."""
    if ws.clips_path.exists():
        doc = review.load(ws)
        return doc, review.clips_for_cutting(doc, clip_ids), True

    if not ws.candidates_path.exists():
        raise StageError(
            "No clips to cut. Run the analyze stage first (no {0} and no {1}).".format(
                ws.clips_path.name, ws.candidates_path.name
            )
        )

    candidates = ws.read_json(ws.candidates_path)
    raw = candidates.get("clips") or []
    clips = []
    for index, item in enumerate(raw, start=1):
        clips.append(
            {
                "id": "c_{0:04d}".format(index),
                "start": float(item["start_time"]),
                "end": float(item["end_time"]),
                "title": "",
                "description": item.get("description", ""),
                "why": item.get("why", ""),
                "status": review.STATUS_APPROVED,
                "output": None,
            }
        )
    if clip_ids:
        wanted = set(clip_ids)
        clips = [c for c in clips if c["id"] in wanted]
    return None, clips, False


def cut_clips(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    clip_ids=None,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Cut each selected clip out of the VOD. Returns the clips directory."""
    source = ws.video_path()
    # A YouTube workspace has no video on disk: cut straight from YouTube.
    resolver = None  # type: Optional[ytsegments.StreamResolver]
    if not source or not source.exists():
        if ws.source_mode() == MODE_EMBED:
            resolver = ytsegments.StreamResolver(ws, settings)
        else:
            raise StageError(
                "No video in {0}. The VOD may have been deleted by the cleanup stage - "
                "re-run the download stage to cut more clips.".format(ws.root)
            )

    doc, clips, using_review = _load_clips(ws, clip_ids)
    if not clips:
        log.warning(
            "Nothing to cut - no approved clips.%s",
            " Approve some in the dashboard first." if using_review else "",
        )
        return ws.clips_dir

    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    # Always re-encoded when cutting from YouTube (see the module docstring).
    re_encode = True if resolver is not None else bool(settings.get("cut.re_encode", False))
    duration = ws.read_state().get("duration")
    ws.clips_dir.mkdir(parents=True, exist_ok=True)
    # Naming what a cached clip was cut from lets a later, better-quality
    # resolution of the same video (HD finishing processing) redo it.
    source_tag = resolver.tag() if resolver is not None else ""

    log.info(
        "Cutting %d clip(s) from %s (%s mode)",
        len(clips),
        "YouTube ({0})".format(resolver.video_id) if resolver is not None else source.name,
        "re-encode" if re_encode else "stream copy",
    )
    progress.phase("cut", total=len(clips), unit="clips")

    cut_count = 0
    skipped = 0
    failed = 0

    for index, clip in enumerate(clips, start=1):
        progress.check_cancelled()
        start, end = _padded_range(clip, settings, duration)
        if end - start < float(settings.get("cut.min_duration", 0.5)):
            log.warning("Clip %s is too short to cut (%.2fs), skipping", clip["id"], end - start)
            failed += 1
            continue

        out_path = ws.clips_dir / "{0}.mp4".format(_clip_label(clip, index))
        fingerprint = _fingerprint(start, end, settings, re_encode, source_tag)
        existing = clip.get("output") or {}

        if (
            not force
            and out_path.exists()
            and existing.get("fingerprint") == fingerprint
        ):
            log.info("  %s unchanged, skipping", out_path.name)
            skipped += 1
            progress.update(index, label=out_path.name)
            continue

        try:
            if resolver is not None:
                ytsegments.fetch_segment(
                    resolver,
                    start,
                    end,
                    out_path,
                    settings,
                    encoder=str(settings.get("cut.encoder", "libx264")),
                    preset=str(settings.get("cut.preset", "veryfast")),
                    crf=int(settings.get("cut.crf", 20)),
                    progress=progress,
                    base=index - 1,
                    span=1.0,
                    log_path=ws.logs_dir / "cut-{0}.log".format(clip["id"]),
                    label=out_path.name,
                )
            else:
                argv = _build_argv(binary, source, out_path, start, end, settings, re_encode)
                run_command(argv, log=log, capture=True)
        except JobCancelled:
            if out_path.exists():
                out_path.unlink()
            raise
        except StageError as exc:
            log.error("  %s failed: %s", out_path.name, exc)
            failed += 1
            if using_review and doc is not None:
                target = review.get_clip(doc, clip["id"])
                if target is not None:
                    target["status"] = review.STATUS_FAILED
                    target["notes"] = str(exc)[:500]
            continue

        if not out_path.exists() or out_path.stat().st_size == 0:
            log.error("  %s produced no output", out_path.name)
            failed += 1
            continue

        size = out_path.stat().st_size
        cut_count += 1
        log.info(
            "  %s  %s - %s  (%s)",
            out_path.name,
            format_timestamp(start),
            format_timestamp(end),
            human_size(size),
        )

        if using_review and doc is not None:
            target = review.get_clip(doc, clip["id"])
            if target is not None:
                target["status"] = review.STATUS_CUT
                target["output"] = {
                    "file": "clips/{0}".format(out_path.name),
                    "bytes": size,
                    "duration": round(end - start, 3),
                    "cut_at": time.time(),
                    "fingerprint": fingerprint,
                    "re_encode": re_encode,
                }
                target["updated_at"] = time.time()

        progress.update(index, label=out_path.name)

    if using_review and doc is not None:
        review.save(ws, doc)

    log.info(
        "Cut %d clip(s)%s%s into %s",
        cut_count,
        ", {0} unchanged".format(skipped) if skipped else "",
        ", {0} failed".format(failed) if failed else "",
        ws.clips_dir,
    )

    if cut_count == 0 and failed and not skipped:
        raise StageError("Every clip failed to cut - see the errors above.")

    ws.mark_stage(
        STAGE,
        clips=cut_count,
        skipped=skipped,
        failed=failed,
        re_encode=re_encode,
    )
    return ws.clips_dir


def uncut_approved(ws: Workspace) -> List[Dict[str, Any]]:
    """Approved clips that haven't been cut yet.

    Used to stop the cleanup stage deleting the VOD while work is outstanding -
    once the video is gone those clips can't be produced without re-downloading.
    """
    if not ws.clips_path.exists():
        return []
    doc = review.load(ws)
    pending = []
    for clip in doc.get("clips") or []:
        if clip.get("status") != review.STATUS_APPROVED:
            continue
        output = clip.get("output") or {}
        target = output.get("file")
        if not target or not (ws.root / target).exists():
            pending.append(clip)
    return pending
