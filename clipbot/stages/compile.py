"""Stage: assemble a named compilation of non-contiguous VOD ranges into one
landscape video.

Separate from cut.py and reel.py on purpose, for the same reason reel is
separate from cut: different output, different idempotence key, different
failure mode. cut.py produces one archival file per clip; reel.py produces
one vertical 9:16 re-encode per clip; this stage produces one landscape video
stitched together from several non-contiguous source ranges - a "supercut".

Segments are cut with `cut_stage._padded_range` (same cut.pad_start/pad_end
settings reel.py already reuses rather than inventing its own padding knob)
but **always re-encoded**, never stream-copied: the concat demuxer's `-c
copy` join step requires every input to share identical codec parameters,
and a source-file stream copy snaps to the nearest keyframe (fine for an
archival clip, not for a boundary that has to land exactly where a highlight
was picked). Each re-encoded segment is cached in a per-compilation scratch
directory and skipped on a re-render if its resolved range is unchanged, the
same skip-if-unchanged discipline cut.py already applies per clip.

**Cross-stream**: each segment carries a `slug` (`clipbot/compilations.py`),
defaulting to the home workspace (the one this compilation's
`compilations.json`/scratch dir/output live in) but able to name any other
workspace instead. `_resolve_source` opens each referenced workspace once
(cached per render) to pull its own video file and its own probed
`state.duration` - padding must clamp against the *source's* duration, not
the home workspace's.
"""

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .. import compilations
from ..config import Settings
from ..ffrun import run_ffmpeg
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import (
    StageError,
    format_timestamp,
    get_logger,
    human_size,
    resolve_tool,
)
from ..workspace import Workspace
from . import cut as cut_stage

log = get_logger(__name__)

STAGE = "compile"

# Named the same way reel.py names its own alias: lets tests patch this one
# name (`mock.patch.object(compile_stage, "_run_ffmpeg", ...)`) without
# reaching into the shared ffrun module and affecting every other stage's
# tests too.
_run_ffmpeg = run_ffmpeg

FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add "
    "the bin folder to PATH or set tools.ffmpeg in config/settings.json."
)


def _encode_settings(settings: Settings) -> Tuple[str, str, int]:
    return (
        str(settings.get("compile.encoder", "libx264")),
        str(settings.get("compile.preset", "slow")),
        int(settings.get("compile.crf", 18)),
    )


def _segment_fingerprint(start: float, end: float, slug: str, settings: Settings) -> str:
    # `slug` is included so two segments from different source workspaces
    # that happen to resolve to an identical numeric range can't collide in
    # the scratch cache.
    encoder, preset, crf = _encode_settings(settings)
    blob = "{0}|{1:.3f}|{2:.3f}|{3}|{4}|{5}".format(slug, start, end, encoder, preset, crf)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _compilation_fingerprint(
    name: str, resolved: List[Tuple[str, float, float]], settings: Settings
) -> str:
    """Hash of everything that determines the joined output.

    Self-maintaining the same way reel.py's argv hash is: any change to a
    segment's resolved (slug, range) or the encode settings invalidates
    automatically, without a hand-listed field tuple to keep in sync.
    """
    encoder, preset, crf = _encode_settings(settings)
    parts = [name] + [
        "{0}:{1:.3f}-{2:.3f}".format(slug, start, end) for slug, start, end in resolved
    ]
    parts += [
        encoder,
        preset,
        str(crf),
        str(settings.get("cut.pad_start", 1.0)),
        str(settings.get("cut.pad_end", 1.5)),
    ]
    blob = "\x1f".join(parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _resolve_source(
    ws: Workspace, settings: Settings, slug: str, cache: Dict[str, Workspace]
) -> Workspace:
    """Open (and cache) the workspace a segment's `slug` names.

    The home workspace (`slug == ws.slug`) is always `ws` itself, never
    re-derived from `settings.work_root / slug` - the CLI's `--workspace`
    accepts an arbitrary path, not just a slug under `work_root`, so `ws`
    may not even live there. Foreign slugs are cached per render so a
    compilation with several segments from the same stream doesn't reopen
    it repeatedly.
    """
    if slug == ws.slug:
        return ws
    if slug not in cache:
        cache[slug] = Workspace(Path(settings.work_root) / slug)
    return cache[slug]


def _segment_argv(
    binary: str, source: Path, out_path: Path, start: float, end: float, settings: Settings
) -> List[str]:
    """Always a re-encode - see the module docstring for why a stream-copy
    cut (cut.py's default) isn't safe input to a concat-demuxer join."""
    encoder, preset, crf = _encode_settings(settings)
    return [
        binary,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-y",
        "-ss",
        format_timestamp(start),
        "-i",
        str(source),
        "-t",
        "{0:.3f}".format(end - start),
        "-c:v",
        encoder,
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]


def _write_concat_list(scratch_dir: Path, filenames: List[str]) -> Path:
    lines = ["ffconcat version 1.0"]
    for fname in filenames:
        lines.append("file {0}".format(fname))
    list_path = scratch_dir / "list.ffconcat"
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


def render_compilation(
    ws: Workspace,
    settings: Settings,
    name: str,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Cut and concatenate one named compilation's segments. Returns the
    rendered file's path."""
    doc = compilations.load(ws)
    comp = compilations.get(doc, name)
    if comp is None:
        raise StageError(
            "No compilation named {0!r}. Define one first (clipbot compile "
            "--name {0} --range start,end,label ...).".format(name)
        )
    segments = comp.get("segments") or []
    if not segments:
        raise StageError("Compilation {0!r} has no segments.".format(name))

    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    min_duration = float(settings.get("compile.min_duration", 0.5))
    source_cache: Dict[str, Workspace] = {}

    resolved: List[Tuple[str, float, float, Path]] = []
    for index, seg in enumerate(segments, start=1):
        slug = seg.get("slug") or ws.slug
        source_ws = _resolve_source(ws, settings, slug, source_cache)
        # video_path() (find_largest_file) assumes the directory exists and
        # raises FileNotFoundError otherwise - real for a foreign `slug`
        # that names a workspace that was never created (typo, or genuinely
        # doesn't exist), unlike the home workspace, which the caller
        # already guaranteed exists.
        source = source_ws.video_path() if source_ws.root.is_dir() else None
        if not source or not source.exists():
            raise StageError(
                "Segment {0} references workspace {1!r}, which has no video - "
                "was it deleted by cleanup? Re-run download there first.".format(
                    index, slug
                )
            )
        duration = source_ws.read_state().get("duration")
        start, end = cut_stage._padded_range(
            {"start": seg["start"], "end": seg["end"]}, settings, duration
        )
        resolved.append((slug, start, end, source))

    fingerprint = _compilation_fingerprint(
        name, [(slug, start, end) for slug, start, end, _ in resolved], settings
    )
    out_path = ws.compilations_dir / "{0}.mp4".format(name)
    existing_output = comp.get("output") or {}

    if not force and out_path.exists() and existing_output.get("fingerprint") == fingerprint:
        log.info("Compilation %r unchanged, skipping", name)
        return out_path

    scratch_dir = ws.compile_scratch_dir(name)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    log.info("Rendering compilation %r (%d segment(s))", name, len(resolved))
    progress.phase("compile", total=len(resolved) + 1, unit="segments")

    segment_files: List[str] = []
    total_span = 0.0

    for index, (slug, start, end, source) in enumerate(resolved, start=1):
        progress.check_cancelled()
        if end - start < min_duration:
            raise StageError(
                "Segment {0} ({1:.2f}s) is shorter than compile.min_duration "
                "({2}s) after padding.".format(index, end - start, min_duration)
            )

        # The scratch filename (a hash) is meaningless to a user watching the
        # job panel - use the segment's own label, same thing the compile page's
        # segment list already displays, so the two match up.
        seg_label = segments[index - 1].get("label") or "segment {0}".format(index)
        seg_fp = _segment_fingerprint(start, end, slug, settings)
        seg_name = "{0:03d}-{1}.mp4".format(index, seg_fp)
        seg_path = scratch_dir / seg_name

        if not seg_path.exists():
            argv = _segment_argv(binary, source, seg_path, start, end, settings)
            seg_log = ws.logs_dir / "compile-{0}-{1:03d}.log".format(name, index)
            try:
                # base=index-1, span=1.0: this segment's own encode progress
                # (0..1) lands within [index-1, index] of the phase total set
                # above, and run_ffmpeg checks progress.cancelled per output
                # line - unlike run_command, a cancel now lands within the
                # segment currently encoding instead of waiting for it to
                # finish on its own.
                _run_ffmpeg(
                    argv, end - start, progress, index - 1, 1.0, seg_log, seg_path,
                    label=seg_label,
                )
            except JobCancelled:
                if seg_path.exists():
                    seg_path.unlink()
                raise
            if not seg_path.exists() or seg_path.stat().st_size == 0:
                raise StageError(
                    "Segment {0} ({1} - {2}) produced no output".format(
                        index, format_timestamp(start), format_timestamp(end)
                    )
                )
            log.info(
                "  segment %d/%d  %s - %s",
                index,
                len(resolved),
                format_timestamp(start),
                format_timestamp(end),
            )
        else:
            log.info("  segment %d/%d unchanged, skipping", index, len(resolved))

        segment_files.append(seg_name)
        total_span += end - start
        progress.update(index, label=seg_label)

    list_path = _write_concat_list(scratch_dir, segment_files)
    ws.compilations_dir.mkdir(parents=True, exist_ok=True)

    join_argv = [
        binary,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    join_log = ws.logs_dir / "compile-{0}-join.log".format(name)
    try:
        # base=len(resolved): the join is the last unit of the phase total
        # (len(resolved) + 1) set above. It's a stream copy, so it finishes
        # in a few seconds regardless - this is mostly for progress-bar
        # consistency with the per-segment encodes above, not because
        # mid-join cancellation latency was ever the actual problem.
        _run_ffmpeg(
            join_argv, total_span, progress, len(resolved), 1.0, join_log, out_path,
            label="joining segments",
        )
    except JobCancelled:
        if out_path.exists():
            out_path.unlink()
        raise

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise StageError("Compilation {0!r} produced no output".format(name))

    size = out_path.stat().st_size
    output = {
        "file": "clips/compilations/{0}.mp4".format(name),
        "bytes": size,
        "duration": round(total_span, 3),
        "rendered_at": time.time(),
        "fingerprint": fingerprint,
    }
    compilations.set_output(ws, name, output)
    # `mark_stage` keys its entry by stage name only - fine for a stage that
    # completes once (download, audio, analyze), but compile runs once per
    # *named* compilation, so a bare mark_stage(STAGE, name=name, ...) would
    # clobber the previous named compilation's record on every re-render.
    # Accumulate per-name details instead, keeping stage_done("compile")'s
    # existing "has this workspace compiled anything" semantics intact.
    stage_state = ws.read_state().get("stages", {}).get(STAGE) or {}
    by_name = dict(stage_state.get("by_name") or {})
    by_name[name] = {"segments": len(resolved), "rendered_at": output["rendered_at"]}
    ws.mark_stage(STAGE, by_name=by_name)
    progress.update(len(resolved) + 1, label=out_path.name)

    log.info(
        "Rendered %r  %.1fs  (%s) -> %s",
        name,
        total_span,
        human_size(size),
        out_path,
    )
    return out_path


def unrendered_compilations(ws: Workspace) -> List[Dict[str, Any]]:
    """Compilations with segments defined whose rendered file doesn't exist yet.

    Mirrors `cut.uncut_approved`/`reel.unrendered_reels`: used to stop the
    cleanup stage deleting the VOD while a compilation is outstanding, since
    every segment is re-encoded from the source video and can't be reproduced
    once it's gone. Like those two, this only checks whether the deliverable
    exists on disk - not whether it's stale relative to the compilation's
    current segments/settings, the same shallow check they make.
    """
    pending = []
    for comp in compilations.list_compilations(ws):
        if not comp.get("segments"):
            continue
        output = comp.get("output") or {}
        target = output.get("file")
        if not target or not (ws.root / target).exists():
            pending.append(comp)
    return pending


def unrendered_compilations_elsewhere(
    ws: Workspace, settings: Settings
) -> List[Dict[str, Any]]:
    """Compilations *homed in other workspaces* that still need `ws`'s
    footage and haven't been rendered yet.

    A cross-stream compilation's segments can be sourced from a workspace
    other than the one its `compilations.json` lives in (see the module
    docstring), so `unrendered_compilations(ws)` alone - which only looks at
    `ws`'s own compilations - isn't enough to know whether deleting `ws`'s
    VOD is safe. Scans every other workspace under `settings.work_root` (same
    "iterate work_root's dirs" pattern `server/app.py`'s `api_workspaces()`
    already uses) for an unrendered compilation with a segment whose `slug`
    resolves to `ws`. Each result names the owning workspace and compilation,
    for a cleanup error message that points somewhere useful.
    """
    pending = []
    root = Path(settings.work_root)
    if not root.is_dir():
        return pending
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("_") or path.name == ws.slug:
            continue
        other = Workspace(path)
        for comp in unrendered_compilations(other):
            segments = comp.get("segments") or []
            if any((seg.get("slug") or other.slug) == ws.slug for seg in segments):
                pending.append({"workspace": other.slug, "compilation": comp.get("name")})
    return pending
