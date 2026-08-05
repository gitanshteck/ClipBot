"""Stage 5b: render approved clips as vertical 9:16 reels.

Separate from the cut stage on purpose. Cutting is a lossless stream copy that
produces an archival 16:9 clip; a reel is a re-encode with a filter graph that
crops gameplay, lifts the webcam out of its overlay corner and composes both onto
a vertical canvas. They have different outputs, different idempotence keys and
different failure modes, so they get different stages and different state keys -
`clip["output"]` for cuts, `clip["reel_output"]` for reels.

Geometry lives entirely in `clipbot/reelspec.py`, shared with the dashboard's
preview endpoint so the preview and the render cannot drift apart.
"""

import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import captionrender
from .. import chatrender
from .. import fxspec
from .. import library
from .. import review
from .. import speakerfx
from .. import speakers as speakers_module
from ..config import Settings
from ..ffrun import run_ffmpeg
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..reelspec import SpecError, build_argv, default_spec, overlay_input_index, resolve
from ..utils import StageError, get_logger, human_size, resolve_tool, slugify
from ..workspace import Workspace
from . import chat as chat_stage
from . import cut as cut_stage

log = get_logger(__name__)

STAGE = "reel"

FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add "
    "the bin folder to PATH or set tools.ffmpeg in config/settings.json."
)


def _reel_name(clip: Dict[str, Any], preset: str) -> str:
    """Stable filename keyed off the clip id.

    Deliberately not reusing cut.py's `_clip_label`, which numbers by position in
    the filtered list - approving an earlier clip renumbers everything after it.
    """
    raw = clip.get("title") or clip.get("description") or ""
    slug = slugify(raw, max_length=40)
    parts = [clip["id"]] + ([slug] if slug and slug != "vod" else []) + [preset]
    return "-".join(parts) + ".mp4"


def _fingerprint(argv: List[str], source_id: str, drop, extra: str = "") -> str:
    """Hash the actual command rather than a hand-listed field tuple.

    cut.py lists its fields by hand and consequently doesn't re-cut when the crf
    changes. Hashing argv is self-maintaining: any change to crop, preset, crf,
    canvas or the filter builder invalidates automatically.

    `extra` covers what argv can't see: the chat frames directory has a fixed
    per-clip path, so a changed offset or a re-fetched chat.json wouldn't
    otherwise touch the command line at all.
    """
    parts = [a for a in argv if a not in drop]
    blob = "\x1f".join(parts) + "\x1f" + source_id + "\x1f" + extra
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _source_id(ws: Workspace, video: Path) -> str:
    state = ws.read_state()
    try:
        size = video.stat().st_size
    except OSError:
        size = 0
    return "{0}:{1}:{2}".format(video.name, size, state.get("video_height"))


# Beyond this the filtergraph goes into a file. Windows caps a command line at
# 32767 characters and a dozen effects with long asset paths can approach it.
FILTER_SCRIPT_THRESHOLD = 8000


def _maybe_script_filter(argv, ws, clip_id, dry_run):
    """Move an oversized -filter_complex into a file. Returns (argv, signature).

    The signature matters as much as the rewrite. `_fingerprint` hashes argv, so
    once the graph is behind a stable filename the argv stops changing when the
    graph does - and a stale reel would be kept forever. Folding the graph's own
    hash into the fingerprint's `extra` restores the property, exactly as the
    chat signature already does for the chat frames.
    """
    try:
        at = argv.index("-filter_complex")
    except ValueError:
        return argv, ""
    graph = argv[at + 1]
    if len(graph) <= FILTER_SCRIPT_THRESHOLD:
        return argv, ""

    signature = hashlib.sha1(graph.encode("utf-8")).hexdigest()[:16]
    path = ws.logs_dir / "fx-{0}.filter".format(clip_id)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(graph, encoding="utf-8")
    argv = list(argv)
    argv[at:at + 2] = ["-filter_complex_script", str(path)]
    return argv, signature


def _has_audio(ws: Workspace, settings: Settings, video: Path) -> bool:
    """Whether the VOD carries an audio stream, cached in state.json.

    `-map 0:a:0?` shrugs at a source with no audio, but `[0:a]` inside a
    filtergraph is a hard failure - so once effects put audio in the graph, this
    has to be known rather than assumed.
    """
    state = ws.read_state()
    cached = state.get("has_audio")
    if isinstance(cached, bool):
        return cached
    try:
        binary = resolve_tool(settings.tool("ffprobe"), FFMPEG_HINT)
        proc = subprocess.run(
            [binary, "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True,
        )
        present = proc.returncode == 0 and bool((proc.stdout or "").strip())
    except Exception as exc:  # noqa: BLE001 - a probe failure must not block
        log.warning("Could not probe for audio (%s); assuming there is some", exc)
        return True
    ws.update_state(has_audio=present)
    return present


def _clips_for_reels(doc: Dict[str, Any], clip_ids=None) -> List[Dict[str, Any]]:
    clips = list(doc.get("clips") or [])
    if clip_ids:
        wanted = set(clip_ids)
        return [c for c in clips if c.get("id") in wanted]
    # Reels are a publishing step - only things the human has blessed.
    return [
        c for c in clips
        if c.get("status") in (review.STATUS_APPROVED, review.STATUS_CUT)
    ]


def _spec_for(clip: Dict[str, Any], ws: Workspace, settings: Settings, preset=None):
    """Per-clip spec, else the workspace default, else the shipped default."""
    spec = clip.get("reel")
    if not spec:
        spec = ws.read_state().get("reel_default")
    if not spec:
        spec = default_spec(settings)
    spec = dict(spec)
    if preset:
        spec["preset"] = preset
        # An explicit preset override implies its geometry, so drop a stored
        # layout/edge that would contradict it.
        spec.pop("layout", None)
        spec.pop("cam_edge", None)
        spec.pop("cam_enabled", None)
    return spec


def _chat_signature(messages: List[Dict[str, Any]], offset: float, style_sig: str) -> str:
    ids = ",".join(m["id"] for m in messages if m.get("id"))
    blob = "{0}\x1f{1}\x1f{2}".format(ids, offset, style_sig)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _chat_window(ws, clip, plan, start, span, chat_doc):
    """This clip's chat messages, re-zeroed onto its own 0..span timeline.

    Cheap and side-effect free on purpose: called to compute the fingerprint
    *before* deciding whether to render anything, so a clip whose fingerprint
    is unchanged never re-renders chat frames just to throw them away.
    """
    chat_plan = plan.get("chat")
    if not chat_plan:
        return None, None

    if chat_doc is None:
        raise StageError(
            "{0} has chat enabled but no usable chat.json. Run: "
            "python -m clipbot chat --workspace {1}".format(clip["id"], ws.slug)
        )

    spec_offset = chat_plan.get("offset")
    state = ws.read_state()
    offset = float(spec_offset) if spec_offset is not None else float(
        state.get("chat_offset_seconds", 0.0)
    )

    # `start`/`span` are already the padded clip range in VOD-time, and the
    # harvest stored offsets in that same VOD-time frame (before calibration),
    # so applying `offset` here - rather than once at fetch time - is what lets
    # a per-clip override exist at all.
    messages = chat_stage.messages_between(chat_doc, start, start + span, offset=offset)
    for message in messages:
        message["offset"] = round(message["offset"] - start, 3)
    return messages, offset


def _captions_signature(segments: List[Dict[str, Any]], offset: float, style_sig: str) -> str:
    ids = ",".join(str(s["id"]) for s in segments if s.get("id") is not None)
    blob = "{0}\x1f{1}\x1f{2}".format(ids, offset, style_sig)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _captions_window(ws, clip, plan, start, span, captions_doc):
    """This clip's caption segments, re-zeroed onto its own 0..span timeline.

    Unlike chat, captions.json times against `transcript.json`, which itself
    times against `audio.wav` - already the VOD's own clock, no
    stream-start-anchor correction needed. `captions.offset` here is purely a
    manual per-clip nudge, mirroring `chat.offset`'s override slot.
    """
    cap_plan = plan.get("captions")
    if not cap_plan:
        return None, None

    if captions_doc is None:
        raise StageError(
            "{0} has captions enabled but no usable captions.json. Run: "
            "python -m clipbot transliterate --workspace {1}".format(clip["id"], ws.slug)
        )

    spec_offset = cap_plan.get("offset")
    offset = float(spec_offset) if spec_offset is not None else 0.0

    segments = []
    for seg in captions_doc.get("segments") or []:
        seg_start = float(seg["start"]) - offset
        seg_end = float(seg["end"]) - offset
        if seg_end <= start or seg_start >= start + span:
            continue
        segments.append({
            "id": seg.get("id"),
            "start": round(seg_start - start, 3),
            "end": round(seg_end - start, 3),
            "text": seg.get("text") or "",
        })
    return segments, offset


# Moved to clipbot/ffrun.py so the dashboard's proxy preview can reuse the same
# progress parsing and, more importantly, the same cancellation path. Aliased
# rather than renamed at the call sites: this stage is the only caller, and the
# name is what the render loop below reads as.
_run_ffmpeg = run_ffmpeg


def render_reels(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    clip_ids=None,
    preset: Optional[str] = None,
    dry_run: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Render vertical reels for the selected clips. Returns the reels directory."""
    video = ws.video_path()
    if not video or not video.exists():
        raise StageError(
            "No video in {0}. Reels re-encode from the VOD, so the download stage "
            "must have run (and cleanup must not have deleted it).".format(ws.root)
        )

    if not ws.clips_path.exists():
        raise StageError(
            "No review state at {0}. Run the analyze stage, then approve some "
            "clips.".format(ws.clips_path)
        )

    doc = review.load(ws)
    clips = _clips_for_reels(doc, clip_ids)
    if not clips:
        log.warning("No approved clips to render. Approve some in the dashboard first.")
        return ws.reels_dir

    state = ws.read_state()
    src_w = int(state.get("video_width") or 0)
    src_h = int(state.get("video_height") or 0)
    if not src_w or not src_h:
        raise StageError(
            "Source dimensions unknown. Open the workspace in the dashboard once "
            "(it probes and records them), or re-run the download stage."
        )

    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    duration = state.get("duration")
    source_id = _source_id(ws, video)
    if not dry_run:
        ws.reels_dir.mkdir(parents=True, exist_ok=True)

    # Loaded once for the whole batch, not per clip: chat.json doesn't change
    # between clips in a single run, and re-parsing a few hundred messages per
    # clip would be pure waste. Best-effort - only clips that actually ask for
    # chat need it to exist, so a missing file shouldn't block plain reels.
    chat_doc = None
    if ws.chat_path.exists():
        try:
            chat_doc = chat_stage.load_chat(ws)
        except StageError as exc:
            log.warning("Chat present but unusable, chat overlays will fail: %s", exc)
    chat_style = chatrender.resolve_style(settings)
    chat_cache = chatrender.ImageCache(
        # Shared across every workspace under work_root - emote/badge artwork
        # doesn't vary per VOD, so there's no reason to refetch it per clip.
        ws.root.parent / "_cache",
        impersonate=str(settings.get("download.impersonate", "chrome") or "chrome"),
    )

    # Same best-effort treatment as chat.json: only clips that actually ask
    # for captions need this to exist, so a missing/unrun transliterate stage
    # must not block a plain reel.
    captions_doc = None
    if ws.captions_path.exists():
        try:
            captions_doc = ws.read_json(ws.captions_path)
        except (ValueError, OSError) as exc:
            log.warning("Captions present but unusable, caption overlays will fail: %s", exc)
    caption_style = captionrender.resolve_style(settings)

    # Same best-effort treatment: only clips with speakers enabled need any
    # of this, so a workspace that never ran diarize/manual assignment must
    # not block a plain reel. Computed once for the whole batch - who spoke
    # when doesn't change per clip, only which window of it is visible.
    all_speaking_spans: List[Dict[str, Any]] = []
    if ws.transcript_path.exists():
        try:
            transcript_for_speakers = ws.read_json(ws.transcript_path)
            speaker_map_doc = speakers_module.load(ws)
            resolved = speakers_module.resolved_segments(transcript_for_speakers, speaker_map_doc)
            all_speaking_spans = speakers_module.speaking_spans(resolved)
        except (ValueError, OSError) as exc:
            log.warning("Speaker assignments present but unusable: %s", exc)
    speaker_registry = {s["id"]: s for s in library.list_speakers(settings)}
    avatar_cache_dir = ws.root.parent / "_cache" / "avatars"

    # The effects library, also loaded once for the batch. It lives outside
    # every workspace, so the same airhorn is reachable from every stream.
    # Best-effort: a clip with no effects must not care that it is missing.
    fx_assets, fx_styles = {}, {}
    try:
        fx_assets = library.asset_map(settings)
        fx_styles = library.load_text_styles(settings)
    except Exception as exc:  # noqa: BLE001 - never block a plain reel
        log.warning("Effects library unavailable: %s", exc)
    fx_font = library.fallback_font(settings)
    has_audio = _has_audio(ws, settings, video)

    log.info(
        "Rendering %d reel(s) from %s (%dx%d)", len(clips), video.name, src_w, src_h
    )
    progress.phase("reel", total=len(clips), unit="clips")

    rendered = skipped = failed = 0

    for index, clip in enumerate(clips, start=1):
        progress.check_cancelled()
        try:
            spec = _spec_for(clip, ws, settings, preset)
            plan = resolve(spec, src_w, src_h, settings)
        except SpecError as exc:
            log.error("  %s has invalid reel settings: %s", clip["id"], exc)
            failed += 1
            continue

        start, end = cut_stage._padded_range(clip, settings, duration)
        span = end - start
        if span < float(settings.get("cut.min_duration", 0.5)):
            log.warning("  %s is too short to render (%.2fs)", clip["id"], span)
            failed += 1
            continue

        try:
            chat_messages, chat_offset = _chat_window(ws, clip, plan, start, span, chat_doc)
        except StageError as exc:
            log.error("  %s: %s", clip["id"], exc)
            failed += 1
            continue

        try:
            caption_segments, caption_offset = _captions_window(
                ws, clip, plan, start, span, captions_doc
            )
        except StageError as exc:
            log.error("  %s: %s", clip["id"], exc)
            failed += 1
            continue

        # Sparse chat (this channel: ~47% of 45s windows are empty) means many
        # chat-enabled clips still have no chat layer at all - the concat input
        # is only added when there's something to draw. Same idea for captions:
        # a clip that's pure gameplay silence has nothing to caption.
        frames_dir = ws.chat_frames_dir(clip["id"])
        chat_list = frames_dir / "list.txt" if chat_messages else None
        chat_sig = (
            _chat_signature(chat_messages, chat_offset, chatrender.style_signature(chat_style))
            if chat_messages else ""
        )

        caption_frames_dir = ws.caption_frames_dir(clip["id"])
        caption_list = caption_frames_dir / "list.txt" if caption_segments else None
        caption_sig = (
            _captions_signature(
                caption_segments, caption_offset, captionrender.style_signature(caption_style)
            )
            if caption_segments else ""
        )

        # Effects (and speaker avatars) are placed/derived at absolute VOD
        # times, so `start` - the padded clip start - is what maps them onto
        # the render's own 0..span timeline. Asset inputs must start after
        # whichever of chat/captions/speakers are actually present, in the
        # same fixed order build_argv adds them.
        overlay_indices = overlay_input_index(chat_list, caption_list)
        try:
            speaker_plan = speakerfx.resolve_speakers(
                plan.get("speakers"), all_speaking_spans, canvas=plan["canvas"],
                origin=start, span_duration=span,
                assets=fx_assets, registry=speaker_registry,
                cache_dir=avatar_cache_dir, next_input=overlay_indices["next"],
                strict=True,
            )
        except SpecError as exc:
            log.error("  %s: %s", clip["id"], exc)
            failed += 1
            target = review.get_clip(doc, clip["id"])
            if target is not None:
                target["reel_error"] = str(exc)[:500]
            continue
        for warning in (speaker_plan or {}).get("warnings") or []:
            log.warning("  %s: %s", clip["id"], warning)
        fx_next_input = overlay_indices["next"] + len((speaker_plan or {}).get("inputs") or [])

        try:
            fx_plan = fxspec.resolve_fx(
                plan.get("fx"), origin=start, span=span, canvas=plan["canvas"],
                assets=fx_assets, styles=fx_styles,
                next_input=fx_next_input,
                strict=True, fallback_font=fx_font,
            )
        except SpecError as exc:
            log.error("  %s: %s", clip["id"], exc)
            failed += 1
            target = review.get_clip(doc, clip["id"])
            if target is not None:
                target["reel_error"] = str(exc)[:500]
            continue
        for warning in (fx_plan or {}).get("warnings") or []:
            log.warning("  %s: %s", clip["id"], warning)

        out_path = ws.reels_dir / _reel_name(clip, plan["preset"])
        argv = build_argv(
            binary, video, out_path, start, span, plan, settings,
            chat_list=chat_list, fx_plan=fx_plan, has_source_audio=has_audio,
            caption_list=caption_list, speaker_plan=speaker_plan,
        )
        argv, graph_sig = _maybe_script_filter(argv, ws, clip["id"], dry_run)
        fingerprint = _fingerprint(
            argv, source_id, {str(video), str(out_path)},
            extra=chat_sig + caption_sig + graph_sig,
        )

        if dry_run:
            print("# {0}  {1}  {2}x{3}  {4:.1f}s{5}{6}{7}{8}".format(
                clip["id"], plan["preset"], plan["canvas"][0], plan["canvas"][1], span,
                "  chat:{0}msg".format(len(chat_messages)) if chat_messages else "",
                "  captions:{0}seg".format(len(caption_segments)) if caption_segments else "",
                "  speakers:{0}".format(len(speaker_plan["video"])) if speaker_plan else "",
                "  fx:{0}".format(len(fx_plan["video"]) + len(fx_plan["audio"]))
                if fx_plan else ""))
            print(" ".join('"{0}"'.format(a) if " " in str(a) else str(a) for a in argv))
            print()
            continue

        previous = clip.get("reel_output") or {}
        if not force and out_path.exists() and previous.get("fingerprint") == fingerprint:
            log.info("  %s unchanged, skipping", out_path.name)
            skipped += 1
            progress.update(index)
            continue

        overlay_render_emptied = False
        if chat_messages:
            rect = plan["chat"]["rect"]
            rendered_list = chatrender.render_frames(
                chat_messages, int(rect[2]), int(rect[3]), span, frames_dir,
                chat_style, cache=chat_cache,
            )
            overlay_render_emptied = overlay_render_emptied or rendered_list is None

        if caption_segments:
            rect = plan["captions"]["rect"]
            rendered_caption_list = captionrender.render_frames(
                caption_segments, int(rect[2]), int(rect[3]), span,
                caption_frames_dir, caption_style,
            )
            overlay_render_emptied = overlay_render_emptied or rendered_caption_list is None

        if overlay_render_emptied:
            # A throttle/rounding edge emptied out a layer whose input index
            # was already baked into fx_plan (and the other overlay's own
            # index) - rebuilding a shifted graph for this rare case isn't
            # worth the risk, so fall back to the plain no-overlay, no-fx
            # command instead, same conservative choice chat already made.
            argv = build_argv(binary, video, out_path, start, span, plan, settings)
            fingerprint = _fingerprint(
                argv, source_id, {str(video), str(out_path)}
            )

        # A retitled clip changes the filename; drop the stale file so orphans
        # don't accumulate in reels/.
        old_file = previous.get("file")
        if old_file:
            old_path = ws.root / old_file
            if old_path.exists() and old_path != out_path:
                try:
                    old_path.unlink()
                except OSError:
                    pass

        up = plan["upscale"]
        log.info(
            "  %s  %s  %dx%d  %.1fs  (gameplay %.2fx%s)",
            clip["id"], plan["preset"], plan["canvas"][0], plan["canvas"][1], span,
            up["game"],
            ", webcam {0:.2f}x".format(up["cam"]) if up["cam"] else "",
        )

        try:
            _run_ffmpeg(
                argv, span, progress, index - 1, 1.0,
                ws.logs_dir / "reel-{0}.log".format(clip["id"]),
                out_path,
            )
        except JobCancelled:
            if out_path.exists():
                out_path.unlink()
            raise
        except StageError as exc:
            log.error("  %s failed: %s", out_path.name, exc)
            if out_path.exists():
                out_path.unlink()
            failed += 1
            target = review.get_clip(doc, clip["id"])
            if target is not None:
                # Deliberately not touching status: that tracks the cut
                # lifecycle, and a reel failure shouldn't change what cut does.
                target["reel_error"] = str(exc)[:500]
            continue
        finally:
            # Scratch output, consumed by the ffmpeg run above and never a
            # deliverable itself - the argv/output are what's worth keeping for
            # debugging a failure, not the frame PNGs.
            if chat_messages and frames_dir.exists():
                shutil.rmtree(frames_dir, ignore_errors=True)
            if caption_segments and caption_frames_dir.exists():
                shutil.rmtree(caption_frames_dir, ignore_errors=True)

        if not out_path.exists() or out_path.stat().st_size == 0:
            log.error("  %s produced no output", out_path.name)
            failed += 1
            continue

        size = out_path.stat().st_size
        rendered += 1
        target = review.get_clip(doc, clip["id"])
        if target is not None:
            target["reel_output"] = {
                "file": "clips/reels/{0}".format(out_path.name),
                "bytes": size,
                # A freeze hold lengthens this and a speed change shortens it,
                # so the recorded duration is the rendered one, not the clip's.
                "duration": round(
                    fx_plan["out_duration"] if fx_plan else span, 3
                ),
                "width": plan["canvas"][0],
                "height": plan["canvas"][1],
                "preset": plan["preset"],
                "upscale": up,
                "source": "vod",
                "rendered_at": time.time(),
                "fingerprint": fingerprint,
            }
            target.pop("reel_error", None)
            target["updated_at"] = time.time()

        log.info("    -> %s (%s)", out_path.name, human_size(size))
        progress.update(index)

    if dry_run:
        return ws.reels_dir

    review.save(ws, doc)

    log.info(
        "Rendered %d reel(s)%s%s into %s",
        rendered,
        ", {0} unchanged".format(skipped) if skipped else "",
        ", {0} failed".format(failed) if failed else "",
        ws.reels_dir,
    )

    if rendered == 0 and failed and not skipped:
        raise StageError("Every reel failed to render - see the errors above.")

    ws.mark_stage(STAGE, reels=rendered, skipped=skipped, failed=failed)
    return ws.reels_dir


def preview_command(
    ws: Workspace,
    settings: Settings,
    clip: Dict[str, Any],
    spec: Dict[str, Any],
    window: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Build the ffmpeg command for a low-resolution proxy of one clip.

    Deliberately assembled from the same `resolve` / `resolve_fx` / `build_argv`
    calls the render uses, with only the canvas and the encoder settings
    changed. Anything more clever here - approximating an effect, skipping a
    layer - would reintroduce exactly the preview/render drift this feature is
    built to avoid.

    The caller sets the destination afterwards, by replacing the last argv
    entry. That ordering is what lets the *key* name the file: the key is a
    fingerprint of the command with the output path excluded, so the path
    cannot be known until the key is.

    Returns a dict carrying `argv`, the fingerprint `key`, and the window, or
    raises SpecError / StageError.
    """
    video = ws.video_path()
    if not video or not video.exists():
        raise StageError("no video to preview from")

    state = ws.read_state()
    src_w = int(state.get("video_width") or 0)
    src_h = int(state.get("video_height") or 0)
    if not src_w or not src_h:
        raise StageError("source dimensions unknown")

    proxy_spec = dict(spec)
    proxy_spec["canvas"] = str(settings.get("reel.preview.canvas", "360x640"))
    plan = resolve(proxy_spec, src_w, src_h, settings)

    clip_start, clip_end = cut_stage._padded_range(clip, settings, state.get("duration"))
    start, span = _preview_window(settings, plan, clip_start, clip_end, window)

    fx_assets = library.asset_map(settings)
    fx_plan = fxspec.resolve_fx(
        plan.get("fx"), origin=start, span=span, canvas=plan["canvas"],
        assets=fx_assets, styles=library.load_text_styles(settings),
        next_input=1, strict=False, fallback_font=library.fallback_font(settings),
    )

    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    placeholder = "<preview-output>"
    argv = build_argv(
        binary, video, placeholder, start, span, plan, settings,
        progress_pipe=True, fx_plan=fx_plan,
        has_source_audio=_has_audio(ws, settings, video),
    )
    argv = _proxy_encoder(argv, settings)

    # The proxy argv differs from a render's in canvas, preset and window, so a
    # preview key can never collide with a reel fingerprint - while any change
    # to the spec, the effects, the assets or the chat offset still invalidates
    # it automatically. That self-maintaining property is the whole reason this
    # reuses _fingerprint rather than hashing a hand-listed tuple of fields.
    key = "pv_" + _fingerprint(
        argv, _source_id(ws, video), {str(video), placeholder}
    )
    return {
        "argv": argv, "key": key, "start": start, "span": span,
        "duration": (fx_plan or {}).get("out_duration", span),
        "warnings": (fx_plan or {}).get("warnings") or [],
        "canvas": plan["canvas"],
    }


def _preview_window(settings, plan, clip_start, clip_end, window):
    """The slice of the clip worth rendering: everything the effects touch.

    Rendering the whole 50-second clip to confirm a 0.4-second punch would make
    the preview useless, so the window is the union of the effect times with a
    little air either side - and just the opening seconds when there are none.
    """
    pad = float(settings.get("reel.preview.pad_seconds", 0.75))
    cap = float(settings.get("reel.preview.max_seconds", 6.0))
    span_all = max(0.1, clip_end - clip_start)

    if window and len(window) == 2:
        start = max(clip_start, min(float(window[0]), clip_end - 0.1))
        end = max(start + 0.1, min(float(window[1]), clip_end))
        return start, min(end - start, cap)

    times = []
    for eff in plan.get("fx") or []:
        if "at" not in eff:
            continue
        times.append(float(eff["at"]))
        times.append(float(eff["at"]) + float(eff.get("dur") or 0.0))
    if not times:
        return clip_start, min(3.0, span_all)

    start = max(clip_start, min(times) - pad)
    end = min(clip_end, max(times) + pad)
    return start, max(0.5, min(end - start, cap))


def _proxy_encoder(argv, settings):
    """Swap the archival encoder settings for fast ones. Geometry is untouched."""
    argv = list(argv)
    replacements = {
        "-preset": str(settings.get("reel.preview.preset_x264", "ultrafast")),
        "-crf": str(settings.get("reel.preview.crf", 28)),
        "-b:a": "96k",
    }
    for flag, value in replacements.items():
        if flag in argv:
            argv[argv.index(flag) + 1] = value
    if "-g" not in argv:
        argv[-1:-1] = ["-g", "15"]
    # Leave a core free: a preview often runs while a real reel is encoding.
    if "-threads" not in argv:
        argv[-1:-1] = ["-threads", "2"]
    return argv


def unrendered_reels(ws: Workspace) -> List[Dict[str, Any]]:
    """Clips carrying reel settings whose reel file doesn't exist yet.

    Used to stop the cleanup stage deleting the VOD while reels are outstanding -
    reels re-encode from the source, so losing it means losing the ability to
    produce them at full quality.
    """
    if not ws.clips_path.exists():
        return []
    doc = review.load(ws)
    pending = []
    for clip in doc.get("clips") or []:
        if not clip.get("reel"):
            continue
        out = clip.get("reel_output") or {}
        target = out.get("file")
        if not target or not (ws.root / target).exists():
            pending.append(clip)
    return pending
