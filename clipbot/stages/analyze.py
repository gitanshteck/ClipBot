"""Stage 4: ask Claude which moments are clip-worthy.

The criteria live entirely in config/rubric.md and config/analysis_prompt.md —
nothing in this module knows what makes a good clip. It formats the transcript,
fills the template, calls the API, and validates the shape of what comes back.

Output is `candidates.json`:
    {
      "model": "claude-sonnet-4-6",
      "rubric_file": "config/rubric.md",
      "rubric_sha1": "...",          # which rubric produced these picks
      "usage": {...},
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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
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


def format_transcript(segments: List[Dict[str, Any]]) -> str:
    """Render segments as `[start - end] text`, flagging low-confidence lines."""
    lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        flag = " (low-confidence)" if seg.get("low_confidence") else ""
        lines.append(
            "[{0:.1f} - {1:.1f}]{2} {3}".format(
                float(seg.get("start", 0.0)), float(seg.get("end", 0.0)), flag, text
            )
        )
    return "\n".join(lines)


def _human_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "an unknown length"
    minutes = seconds / 60.0
    if minutes < 90:
        return "{0:.0f} minutes".format(minutes)
    return "{0:.1f} hours".format(minutes / 60.0)


def build_prompt(
    transcript: Dict[str, Any],
    settings: Settings,
    state: Dict[str, Any],
) -> Tuple[str, str, str]:
    """Return (cached_part, live_part, rubric_sha1).

    The template is split at CACHE_MARKER: the transcript half is marked
    cacheable so re-running with an edited rubric only re-bills the rubric.
    """
    rubric_path = settings.project_path("analyze.rubric_file")
    template_path = settings.project_path("analyze.prompt_template")

    rubric = _strip_html_comments(_load_text(rubric_path, "Rubric file"))
    template = _strip_html_comments(_load_text(template_path, "Prompt template"))
    rubric_sha1 = hashlib.sha1(rubric.encode("utf-8")).hexdigest()

    segments = transcript.get("segments") or []
    if not segments:
        raise StageError("Transcript has no segments - nothing to analyze.")

    rendered = template
    for placeholder, value in (
        ("{{TRANSCRIPT}}", format_transcript(segments)),
        ("{{RUBRIC}}", rubric),
        ("{{DURATION}}", _human_duration(transcript.get("duration"))),
        ("{{STREAM_TITLE}}", str(state.get("title") or "untitled stream")),
    ):
        rendered = rendered.replace(placeholder, value)

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
    cached_part, live_part, rubric_sha1 = build_prompt(transcript, settings, state)

    client = _client(settings)
    model = str(settings.get("analyze.model", "claude-sonnet-4-6"))
    max_tokens = int(settings.get("analyze.max_tokens", 16000))

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

    try:
        counted = client.messages.count_tokens(
            model=model, messages=request["messages"]
        )
        log.info(
            "Sending %d segments (~%d input tokens) to %s",
            len(transcript.get("segments") or []),
            counted.input_tokens,
            model,
        )
    except Exception as exc:  # token counting is informational only
        log.debug("count_tokens failed: %s", exc)
        log.info("Sending %d segments to %s", len(transcript.get("segments") or []), model)

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
    clips = validate_clips(payload.get("clips"), transcript.get("duration"), settings)

    usage = getattr(response, "usage", None)
    usage_dict = {}
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
            "transcript_segments": len(transcript.get("segments") or []),
            "stop_reason": getattr(response, "stop_reason", None),
            "usage": usage_dict,
            "clips": clips,
        },
    )
    log.info("Wrote %s", out_path)

    ws.mark_stage(STAGE, model=model, clips=len(clips), rubric_sha1=rubric_sha1)
    return out_path
