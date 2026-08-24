"""Stage 4: ask Claude which moments are clip-worthy.

The criteria live entirely in config/rubric.md and config/analysis_prompt.md —
nothing in this module knows what makes a good clip. It formats the transcript,
fills the template, calls the API, and validates the shape of what comes back.

**Long transcripts are analyzed in overlapping windows, not one call.**
Measured on a real 5.1h/4148-segment stream: a single call found clips densely
in the first ~3 hours, then went 83 minutes without picking anything, despite
stopping voluntarily at `stop_reason="end_turn"` using under 6% of its output
budget — a "lost in the middle" long-context recall problem, not a token-limit
problem. `analyze.chunk_threshold_minutes` gates this: streams shorter than the
threshold still get exactly one call, byte-identical prompt to before this
existed. Longer streams get split into `analyze.chunk_minutes`-sized windows
with `analyze.chunk_overlap_minutes` of context padding on each side; each
window's prompt explicitly instructs the model to only propose clips starting
inside its own "core" range (the padding is for setup context only), which
heads off most cross-window duplicates before they're ever generated. Windows
run through a small thread pool (`analyze.concurrency`) — the same
network-I/O-in-threads reasoning `server/jobs.py` already documents.

**Notable-moment signals** (`clipbot/highlights.py`) annotate transcript lines
with `(energy spike)` / `(chat spike, Nx)` tags derived from audio loudness and
chat message rate — independent of the transcribed words, so a moment that's
pure laughter or a loud reaction with no distinctive dialogue is no longer
invisible to the model. Applies on every run, chunked or not.

Output is `candidates.json`:
    {
      "model": "claude-sonnet-4-6",
      "rubric_file": "config/rubric.md",
      "rubric_sha1": "...",          # which rubric produced these picks
      "chunked": false,
      "chunk_count": 1,
      "signal_count": 0,
      "usage": {...},                # summed across all chunk calls
      "clips": [
        {"start_time": 1234.5, "end_time": 1271.0,
         "description": "...", "why": "..."}
      ]
    }
"""

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..highlights import notable_moments
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import StageError, get_logger
from ..workspace import Workspace

log = get_logger(__name__)

STAGE = "analyze"

CACHE_MARKER = "{{CACHE_BREAKPOINT}}"

API_KEY_HINT = (
    "Set your API key first:\n"
    '  PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."\n'
    "  Persist it:  setx ANTHROPIC_API_KEY \"sk-ant-...\"  (then reopen the shell)"
)


def _load_text(path: Path, label: str) -> str:
    if not path.exists():
        raise StageError("{0} not found: {1}".format(label, path))
    return path.read_text(encoding="utf-8-sig")


def _strip_html_comments(text: str) -> str:
    """Drop the <!-- ... --> editing notes at the top of the template files."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def format_transcript(
    segments: List[Dict[str, Any]], signals: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Render segments as `[start - end] text`, flagging low-confidence lines
    and, if `signals` is given, any line overlapping a notable-moment span
    from `highlights.notable_moments` (energy/chat spikes). With no signals,
    output is identical to the pre-signals format - only `low-confidence`
    ever tags a line.
    """
    signals = signals or []
    lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))

        tags = []
        if seg.get("low_confidence"):
            tags.append("low-confidence")
        for sig in signals:
            if seg_start < sig["end"] and seg_end > sig["start"]:
                tags.append(sig["label"])
        flag = " ({0})".format(", ".join(tags)) if tags else ""

        lines.append("[{0:.1f} - {1:.1f}]{2} {3}".format(seg_start, seg_end, flag, text))
    return "\n".join(lines)


def _human_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "an unknown length"
    minutes = seconds / 60.0
    if minutes < 90:
        return "{0:.0f} minutes".format(minutes)
    return "{0:.1f} hours".format(minutes / 60.0)


def _window_note(core_start: float, core_end: float, index: int, total: int) -> str:
    return (
        "This is part {0}/{1} of a longer transcript, covering roughly "
        "{2:.0f}-{3:.0f} minutes of the stream (a little extra transcript "
        "before and after is included only for setup context). Only propose "
        "clips whose start_time falls between {4:.0f}s and {5:.0f}s - "
        "moments outside that range belong to a different part of this "
        "pass and will be considered there.".format(
            index, total, core_start / 60.0, core_end / 60.0, core_start, core_end
        )
    )


def build_prompt(
    transcript: Dict[str, Any],
    settings: Settings,
    state: Dict[str, Any],
    segments: Optional[List[Dict[str, Any]]] = None,
    window_note: str = "",
    signals: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, str]:
    """Return (cached_part, live_part, rubric_sha1).

    The template is split at CACHE_MARKER: the transcript half is marked
    cacheable so re-running with an edited rubric only re-bills the rubric.

    `segments` overrides `transcript["segments"]` (used for a chunked
    window's padded slice); `window_note` fills `{{WINDOW_NOTE}}` (empty for
    a normal, non-chunked call - the template renders byte-identical to
    before chunking existed); `signals` are passed through to
    `format_transcript` for the energy/chat-spike annotations.
    """
    rubric_path = settings.project_path("analyze.rubric_file")
    template_path = settings.project_path("analyze.prompt_template")

    rubric = _strip_html_comments(_load_text(rubric_path, "Rubric file"))
    template = _strip_html_comments(_load_text(template_path, "Prompt template"))
    rubric_sha1 = hashlib.sha1(rubric.encode("utf-8")).hexdigest()

    segs = segments if segments is not None else (transcript.get("segments") or [])
    if not segs:
        raise StageError("Transcript has no segments - nothing to analyze.")

    rendered = template
    for placeholder, value in (
        ("{{TRANSCRIPT}}", format_transcript(segs, signals=signals)),
        ("{{RUBRIC}}", rubric),
        ("{{DURATION}}", _human_duration(transcript.get("duration"))),
        ("{{STREAM_TITLE}}", str(state.get("title") or "untitled stream")),
        ("{{WINDOW_NOTE}}", window_note),
    ):
        rendered = rendered.replace(placeholder, value)

    # A blank {{WINDOW_NOTE}} otherwise leaves a trailing space before the
    # line break on a non-chunked run - trim per-line so that path's prompt
    # stays as close to byte-identical as edits elsewhere in this function
    # allow, which matters for prompt-cache stability.
    rendered = "\n".join(line.rstrip() for line in rendered.split("\n"))

    if CACHE_MARKER in rendered:
        cached, live = rendered.split(CACHE_MARKER, 1)
        return cached.strip(), live.strip(), rubric_sha1

    log.debug("No %s in template; sending as a single uncached block", CACHE_MARKER)
    return "", rendered, rubric_sha1


def extract_json(text: str) -> Dict[str, Any]:
    """Parse the model's reply as JSON, tolerating fences and stray prose."""
    candidate = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, flags=re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        return json.loads(candidate)
    except ValueError:
        pass

    # Fall back to the outermost {...} span.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except ValueError as exc:
            raise StageError(
                "Could not parse JSON from the model response: {0}\n"
                "First 500 chars:\n{1}".format(exc, text[:500])
            )
    raise StageError(
        "No JSON object found in the model response.\nFirst 500 chars:\n{0}".format(
            text[:500]
        )
    )


def validate_clips(
    raw_clips: Any,
    duration: Optional[float],
    settings: Settings,
) -> List[Dict[str, Any]]:
    """Structural validation only — the rubric owns the editorial criteria.

    Drops entries that can't be cut (non-numeric, zero-length, out of range) and
    trims overlaps. Duration outliers are warned about, not dropped: how long a
    clip should be is a rubric question, not a code question.

    Also the merge point for the chunked path: each window's clips are
    concatenated before reaching here, so this is what catches any residual
    cross-window overlap the core/context window split didn't already avoid.
    """
    if not isinstance(raw_clips, list):
        raise StageError(
            "Expected 'clips' to be a list, got {0}".format(type(raw_clips).__name__)
        )

    warn_min = float(settings.get("analyze.warn_shorter_than", 5.0))
    warn_max = float(settings.get("analyze.warn_longer_than", 120.0))

    clean: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_clips):
        if not isinstance(item, dict):
            log.warning("Clip %d is not an object, skipping", index)
            continue
        try:
            start = float(item["start_time"])
            end = float(item["end_time"])
        except (KeyError, TypeError, ValueError):
            log.warning("Clip %d has missing/non-numeric times, skipping: %r", index, item)
            continue

        if end <= start:
            log.warning("Clip %d ends before it starts (%.1f -> %.1f), skipping", index, start, end)
            continue

        start = max(0.0, start)
        if duration:
            if start >= duration:
                log.warning("Clip %d starts past the end of the stream, skipping", index)
                continue
            if end > duration:
                log.warning("Clip %d ends past the stream, trimming to %.1f", index, duration)
                end = duration

        length = end - start
        if length < warn_min:
            log.warning("Clip %d is only %.1fs long", index, length)
        elif length > warn_max:
            log.warning("Clip %d is %.1fs long - longer than the rubric asks for", index, length)

        clean.append(
            {
                "start_time": round(start, 2),
                "end_time": round(end, 2),
                "description": str(item.get("description") or "").strip(),
                "why": str(item.get("why") or "").strip(),
            }
        )

    clean.sort(key=lambda c: c["start_time"])

    # Trim overlaps so the cutting stage never produces duplicated footage.
    deduped: List[Dict[str, Any]] = []
    for clip in clean:
        if deduped and clip["start_time"] < deduped[-1]["end_time"]:
            previous = deduped[-1]
            log.warning(
                "Clips overlap (%.1f-%.1f and %.1f-%.1f); trimming the second",
                previous["start_time"],
                previous["end_time"],
                clip["start_time"],
                clip["end_time"],
            )
            clip["start_time"] = previous["end_time"]
            if clip["end_time"] - clip["start_time"] < 1.0:
                log.warning("  nothing left after trimming, dropping it")
                continue
        deduped.append(clip)

    return deduped


def _client(settings: Settings):
    try:
        import anthropic
    except ImportError as exc:
        raise StageError(
            "The anthropic SDK is not installed.\nInstall with: pip install -U anthropic"
        ) from exc

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise StageError("ANTHROPIC_API_KEY is not set.\n" + API_KEY_HINT)

    return anthropic.Anthropic()


def _call_model(
    client,
    model: str,
    max_tokens: int,
    settings: Settings,
    transcript: Dict[str, Any],
    state: Dict[str, Any],
    segments: List[Dict[str, Any]],
    window_note: str,
    signals: List[Dict[str, Any]],
    progress: Progress,
) -> Tuple[List[Any], Dict[str, int], Optional[str], str]:
    """One Claude call (one window, or the whole transcript). Returns
    (raw_clips, usage_dict, stop_reason, rubric_sha1). Raises StageError on
    unrecoverable failures (refusal, no text, max_tokens-with-no-text)."""
    cached_part, live_part, rubric_sha1 = build_prompt(
        transcript, settings, state, segments=segments, window_note=window_note, signals=signals,
    )

    content: List[Dict[str, Any]] = []
    if cached_part:
        content.append(
            {
                "type": "text",
                "text": cached_part,
                # Cache the transcript half: re-running with an edited rubric
                # then re-bills only the rubric, not the whole VOD.
                "cache_control": {"type": "ephemeral"},
            }
        )
    content.append({"type": "text", "text": live_part})

    request: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if settings.get("analyze.thinking", True):
        request["thinking"] = {"type": "adaptive"}
    effort = settings.get("analyze.effort")
    if effort:
        request["output_config"] = {"effort": str(effort)}

    # Streaming, not create(): thinking tokens count against max_tokens, and on a
    # full-length transcript the model can spend the whole budget reasoning and
    # return no text at all. A large max_tokens also needs streaming to avoid
    # HTTP timeouts on a multi-minute request.
    try:
        with client.messages.stream(**request) as stream:
            for _ in stream.text_stream:
                progress.check_cancelled()
            response = stream.get_final_message()
    except JobCancelled:
        raise
    except Exception as exc:
        raise StageError("Claude API call failed: {0}".format(exc))

    if getattr(response, "stop_reason", None) == "refusal":
        raise StageError(
            "The model declined this request (stop_reason=refusal). "
            "Check the rubric and transcript for content that may have tripped a filter."
        )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    hit_cap = getattr(response, "stop_reason", None) == "max_tokens"

    if hit_cap and not text.strip():
        # Thinking tokens share the max_tokens budget. On a long transcript at
        # high effort the model can spend the entire budget reasoning and never
        # start the answer - raising max_tokens does not fix this, it just makes
        # the failure more expensive.
        raise StageError(
            "The model used its entire {0}-token budget without producing any "
            "output.\nThis is a thinking-budget problem, not a size problem: "
            "reasoning tokens count against max_tokens.\n"
            "In config/settings.json try analyze.thinking=false, or lower "
            "analyze.effort to \"low\"/\"medium\".\n"
            "(thinking={1}, effort={2})".format(
                max_tokens,
                settings.get("analyze.thinking", True),
                settings.get("analyze.effort"),
            )
        )
    if hit_cap:
        log.warning(
            "Response hit max_tokens (%d); the clip list may be truncated.", max_tokens
        )
    if not text.strip():
        raise StageError(
            "Model returned no text (stop_reason={0})".format(response.stop_reason)
        )

    payload = extract_json(text)
    raw_clips = payload.get("clips")
    if not isinstance(raw_clips, list):
        raise StageError(
            "Expected 'clips' to be a list, got {0}".format(type(raw_clips).__name__)
        )

    usage = getattr(response, "usage", None)
    usage_dict: Dict[str, int] = {}
    if usage is not None:
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = getattr(usage, field, None)
            if value is not None:
                usage_dict[field] = value

    return raw_clips, usage_dict, getattr(response, "stop_reason", None), rubric_sha1


def _build_windows(
    duration: float, chunk_minutes: float, overlap_minutes: float
) -> List[Tuple[float, float, float, float]]:
    """[(core_start, core_end, pad_start, pad_end), ...] covering the full
    duration. `pad_*` extends `overlap_minutes` past each core boundary
    (clamped to the stream's own range) purely for setup context."""
    chunk_seconds = chunk_minutes * 60.0
    overlap_seconds = overlap_minutes * 60.0
    windows = []
    core_start = 0.0
    while core_start < duration:
        core_end = min(duration, core_start + chunk_seconds)
        pad_start = max(0.0, core_start - overlap_seconds)
        pad_end = min(duration, core_end + overlap_seconds)
        windows.append((core_start, core_end, pad_start, pad_end))
        core_start = core_end
    return windows


def analyze_transcript(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Send the transcript to Claude and write candidates.json."""
    out_path = ws.candidates_path
    if out_path.exists() and not force:
        log.info("Candidates already exist, skipping: %s", out_path.name)
        return out_path

    if not ws.transcript_path.exists():
        raise StageError(
            "No transcript at {0}. Run the transcribe stage first.".format(
                ws.transcript_path
            )
        )

    transcript = ws.read_json(ws.transcript_path)
    state = ws.read_state()
    duration = float(transcript.get("duration") or 0.0)
    all_segments = transcript.get("segments") or []
    if not all_segments:
        raise StageError("Transcript has no segments - nothing to analyze.")

    client = _client(settings)
    model = str(settings.get("analyze.model", "claude-sonnet-4-6"))
    max_tokens = int(settings.get("analyze.max_tokens", 16000))

    try:
        signals = notable_moments(ws, settings)
    except Exception as exc:  # a broken signal must not sink the analyze stage
        log.warning("Notable-moment signal computation failed: %s", exc)
        signals = []
    if signals:
        log.info("Computed %d notable-moment signal span(s)", len(signals))

    threshold_seconds = float(settings.get("analyze.chunk_threshold_minutes", 90)) * 60.0
    chunk_minutes = float(settings.get("analyze.chunk_minutes", 60))
    overlap_minutes = float(settings.get("analyze.chunk_overlap_minutes", 8))
    concurrency = max(1, int(settings.get("analyze.concurrency", 2)))

    windows = _build_windows(duration, chunk_minutes, overlap_minutes) if duration > threshold_seconds else None

    rubric_sha1 = ""
    usage_dict: Dict[str, int] = {}
    raw_clips: List[Any] = []
    stop_reason_overall: Optional[str] = None
    chunked = bool(windows)
    chunk_count = len(windows) if windows else 1

    if windows:
        log.info(
            "Transcript is %.1f min (> %.0f min threshold) - analyzing in %d "
            "overlapping window(s) of ~%.0f min",
            duration / 60.0, threshold_seconds / 60.0, len(windows), chunk_minutes,
        )
        log.info("Sending %d segments across %d window(s) to %s", len(all_segments), len(windows), model)

        def _run_window(item):
            index, (core_start, core_end, pad_start, pad_end) = item
            segs = [
                s for s in all_segments
                if pad_start <= float(s.get("start", 0.0)) < pad_end
            ]
            note = _window_note(core_start, core_end, index + 1, len(windows))
            try:
                clips, usage, stop_reason, sha1 = _call_model(
                    client, model, max_tokens, settings, transcript, state,
                    segs, note, signals, progress,
                )
                return index, clips, usage, stop_reason, sha1, None
            except JobCancelled:
                raise
            except Exception as exc:
                return index, [], {}, None, "", exc

        results: List[Tuple[int, List[Any], Dict[str, int], Optional[str], str]] = []
        errors = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for index, clips, usage, stop_reason, sha1, exc in pool.map(
                _run_window, list(enumerate(windows))
            ):
                progress.check_cancelled()
                if exc is not None:
                    errors += 1
                    log.warning("  window %d/%d failed: %s", index + 1, len(windows), exc)
                else:
                    results.append((index, clips, usage, stop_reason, sha1))
                    if sha1:
                        rubric_sha1 = sha1
                progress.update(len(results) + errors, len(windows))

        results.sort(key=lambda r: r[0])
        for _, clips, usage, stop_reason, _sha1 in results:
            raw_clips.extend(clips)
            for k, v in usage.items():
                usage_dict[k] = usage_dict.get(k, 0) + v
            if stop_reason == "max_tokens":
                stop_reason_overall = "max_tokens"
        if stop_reason_overall is None:
            stop_reason_overall = "end_turn"
        if errors:
            log.warning("%d/%d window(s) failed and were skipped", errors, len(windows))
        if not results:
            raise StageError("All {0} analysis window(s) failed - nothing to write.".format(len(windows)))
    else:
        progress.phase("analyze", total=1, unit="calls")
        try:
            cached_part, live_part, _ = build_prompt(transcript, settings, state, signals=signals)
            probe_content: List[Dict[str, Any]] = []
            if cached_part:
                probe_content.append({"type": "text", "text": cached_part})
            probe_content.append({"type": "text", "text": live_part})
            counted = client.messages.count_tokens(
                model=model, messages=[{"role": "user", "content": probe_content}],
            )
            log.info(
                "Sending %d segments (~%d input tokens) to %s",
                len(all_segments), counted.input_tokens, model,
            )
        except Exception as exc:  # token counting is informational only
            log.debug("count_tokens failed: %s", exc)
            log.info("Sending %d segments to %s", len(all_segments), model)

        raw_clips, usage_dict, stop_reason_overall, rubric_sha1 = _call_model(
            client, model, max_tokens, settings, transcript, state,
            all_segments, "", signals, progress,
        )
        progress.update(1, 1)

    clips = validate_clips(raw_clips, duration, settings)

    cached_read = usage_dict.get("cache_read_input_tokens") or 0
    if cached_read:
        log.info("Reused %d cached input tokens", cached_read)

    log.info("Claude returned %d candidate clip(s)", len(clips))
    for clip in clips:
        log.info(
            "  %6.1fs-%6.1fs  %s",
            clip["start_time"],
            clip["end_time"],
            clip["description"][:70],
        )

    ws.write_json(
        out_path,
        {
            "model": model,
            "rubric_file": str(settings.get("analyze.rubric_file")),
            "rubric_sha1": rubric_sha1,
            "transcript_segments": len(all_segments),
            "chunked": chunked,
            "chunk_count": chunk_count,
            "signal_count": len(signals),
            "stop_reason": stop_reason_overall,
            "usage": usage_dict,
            "clips": clips,
        },
    )
    log.info("Wrote %s", out_path)

    ws.mark_stage(
        STAGE, model=model, clips=len(clips), rubric_sha1=rubric_sha1,
        chunked=chunked, chunk_count=chunk_count,
    )
    return out_path
