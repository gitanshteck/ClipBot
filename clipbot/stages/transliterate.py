"""Stage 3c: convert the Devanagari transcript to Hinglish (Latin script).

`transcribe.py` pins `language: "hi"`, so Whisper writes spoken Hindi as
Devanagari. `chatrender.py` already documents that Pillow (no libraqm in any
published Windows wheel) cannot shape Devanagari correctly - so burning the
transcript in as-is was never an option for an on-video caption overlay.
Converting it to Hinglish, the same way it would be typed in chat, is what
makes `captionrender.py` possible at all.

This is a Claude-calling stage shaped like `analyze.py`, not a local-model
stage shaped like `transcribe.py`: it's a text-in/text-out API call, not a
model held resident in memory. Segments are batched (`transliterate.batch_size`)
because a multi-hour VOD can have 800+ segments and one call for all of them
would be a single point of failure for the whole transcript; a bad batch is
logged and its segments fall back to the original Devanagari text rather than
failing the run, same tolerant-of-partial-failure spirit as
`library.fetch_starter`.

captions.json:
    {
      "source_transcript_sha1": "...",
      "model": "claude-haiku-4-5",
      "batch_size": 50,
      "segment_count": 812,
      "failed_batches": 0,
      "segments": [
        {"id": 0, "start": 12.34, "end": 15.02, "text": "kya baat hai bhai"}
      ]
    }
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import StageError, get_logger
from ..workspace import Workspace

log = get_logger(__name__)

STAGE = "transliterate"

API_KEY_HINT = (
    "Set your API key first:\n"
    '  PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."\n'
    "  Persist it:  setx ANTHROPIC_API_KEY \"sk-ant-...\"  (then reopen the shell)"
)

PROMPT_HEADER = """\
You convert Hindi speech transcripts (written in Devanagari script) into \
Hinglish - the same words written in casual Latin script, exactly the way a \
Hindi-English bilingual streamer or viewer types it in chat (e.g. "kya baat \
hai", "nahi yaar", "bhai kya kar raha hai").

Rules:
- Produce casual Latin-script Hinglish spelling, the way people actually \
type it - not formal IAST/ITRANS transliteration, no diacritics.
- English words or phrases already in the source (including English \
code-switched into a Hindi sentence) are copied through unchanged.
- Transliterate only - do not translate, summarize, correct grammar, or \
paraphrase.
- Preserve each segment's "id" exactly as given.
- If a segment is already fully in English, return it unchanged.

Return ONLY a JSON array, no prose, no markdown fence, one entry per input \
segment:
[{"id": 0, "text": "..."}, ...]

Segments:
"""


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


def _extract_json_array(text: str) -> List[Any]:
    """Tolerant JSON-array parser, same shape as analyze.py's extract_json
    (fenced blocks, stray prose) but for a top-level array instead of an
    object."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, flags=re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("segments"), list):
            return parsed["segments"]
    except ValueError:
        pass
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
    raise StageError(
        "No JSON array found in the model response.\nFirst 300 chars:\n{0}".format(
            text[:300]
        )
    )


def _transliterate_batch(
    client, model: str, max_tokens: int, thinking: bool, batch: List[Dict[str, Any]]
) -> Dict[int, str]:
    """One API call for one batch of {id, text}. Returns id -> hinglish text.

    Missing/unparseable results are simply absent from the returned dict -
    the caller falls back to the original Devanagari for those ids rather
    than failing the whole stage over one bad batch.
    """
    payload = json.dumps(
        [{"id": s["id"], "text": s["text"]} for s in batch], ensure_ascii=False
    )
    request: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": PROMPT_HEADER + payload}],
    }
    if thinking:
        request["thinking"] = {"type": "adaptive"}

    # Streaming, not create(): same reasoning-tokens-eat-max_tokens failure
    # mode analyze.py already hit and worked around.
    with client.messages.stream(**request) as stream:
        for _ in stream.text_stream:
            pass
        response = stream.get_final_message()

    if getattr(response, "stop_reason", None) == "refusal":
        raise StageError("The model declined this batch (stop_reason=refusal).")
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise StageError(
            "Model returned no text for this batch (stop_reason={0})".format(
                getattr(response, "stop_reason", None)
            )
        )

    out: Dict[int, str] = {}
    for item in _extract_json_array(text):
        if not isinstance(item, dict):
            continue
        try:
            seg_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        hinglish = str(item.get("text") or "").strip()
        if hinglish:
            out[seg_id] = hinglish
    return out


def transliterate_transcript(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Convert transcript.json into Hinglish captions.json. Returns the path."""
    out_path = ws.captions_path
    if out_path.exists() and not force:
        log.info("Captions already exist, skipping: %s", out_path.name)
        return out_path

    if not ws.transcript_path.exists():
        raise StageError(
            "No transcript at {0}. Run the transcribe stage first.".format(
                ws.transcript_path
            )
        )

    transcript = ws.read_json(ws.transcript_path)
    segments = [s for s in (transcript.get("segments") or []) if (s.get("text") or "").strip()]
    if not segments:
        raise StageError("Transcript has no non-empty segments - nothing to transliterate.")

    transcript_sha1 = hashlib.sha1(
        json.dumps(
            [(s["id"], s["text"]) for s in segments], ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()

    client = _client(settings)
    model = str(settings.get("transliterate.model", "claude-haiku-4-5"))
    max_tokens = int(settings.get("transliterate.max_tokens", 4096))
    thinking = bool(settings.get("transliterate.thinking", False))
    batch_size = max(1, int(settings.get("transliterate.batch_size", 50)))

    batches = [segments[i : i + batch_size] for i in range(0, len(segments), batch_size)]
    log.info(
        "Transliterating %d segments in %d batch(es) of up to %d via %s",
        len(segments), len(batches), batch_size, model,
    )

    progress.phase("transliterate", total=len(batches), unit="batches")
    results: Dict[int, str] = {}
    failed_batches = 0

    for index, batch in enumerate(batches, start=1):
        progress.check_cancelled()
        try:
            results.update(_transliterate_batch(client, model, max_tokens, thinking, batch))
        except JobCancelled:
            raise
        except Exception as exc:  # a bad batch must not sink the whole run
            failed_batches += 1
            log.warning(
                "  batch %d/%d failed (%s); those %d segment(s) keep their "
                "original Devanagari text",
                index, len(batches), exc, len(batch),
            )
        progress.update(index)

    out_segments = []
    for seg in segments:
        text = results.get(seg["id"], seg["text"])
        out_segments.append(
            {"id": seg["id"], "start": seg["start"], "end": seg["end"], "text": text}
        )

    payload = {
        "source_transcript_sha1": transcript_sha1,
        "model": model,
        "batch_size": batch_size,
        "segment_count": len(out_segments),
        "failed_batches": failed_batches,
        "segments": out_segments,
    }
    ws.write_json(out_path, payload)
    log.info(
        "Wrote %s (%d segments, %d failed batch(es))",
        out_path, len(out_segments), failed_batches,
    )

    ws.mark_stage(
        STAGE, model=model, segments=len(out_segments), failed_batches=failed_batches
    )
    return out_path
