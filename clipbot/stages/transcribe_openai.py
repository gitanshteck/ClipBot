"""Stage 3 (alternate backend): transcribe audio via OpenAI's hosted API.

`transcribe.py`'s docstring already frames the local faster-whisper path as
swappable - "the contract downstream stages rely on is the JSON schema,
nothing more" - and this is that swap. Selected via `transcribe.backend`
("openai", the default) or a per-call `backend` override; see
`transcribe.transcribe_audio`'s dispatcher.

Deliberately whisper-1, not gpt-4o-transcribe/gpt-4o-mini-transcribe: only
whisper-1's `verbose_json` response carries the per-segment `avg_logprob`/
`no_speech_prob`/`compression_ratio` this pipeline's low-confidence flagging
already depends on (calibrated in transcribe.py). The newer models only
support `response_format=json` with per-token logprobs, a different shape
that the existing thresholds can't be applied to.

Two problems the local path never has to solve, because faster-whisper reads
the whole file itself:
  - OpenAI's transcription endpoint caps uploads at 25MB. A multi-hour
    16kHz mono PCM `audio.wav` is hundreds of MB, so this stage transcodes
    to a compressed format and splits it into fixed-length chunks first.
  - Chunk boundaries are plain fixed-time cuts, not silence-aware, so a word
    can occasionally split across a chunk edge. Not engineered around on
    purpose: the existing low-confidence flagging already exists to catch
    exactly this kind of artifact (a boundary-mangled segment scores a low
    avg_logprob and gets flagged, never silently trusted), so it doesn't
    need a second, bespoke fix.

Chunks upload with bounded thread-pool parallelism - network-bound calls in
threads, the same justification `server/jobs.py` already gives for using
threads over processes ("Claude calls are network I/O"). This is the actual
"quicker run" mechanism versus the local path's single-threaded ~0.8x
realtime.

transcript.json: same shape as transcribe.py's, plus:
  - "backend": "openai"
  - "failed_chunks": count of chunks that never produced text after retries
    (each contributes one synthetic low_confidence segment spanning its
    time range instead of aborting the whole run - same
    count-failures-don't-abort precedent transliterate.py's
    "failed_batches" already sets).
"""

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import StageError, get_logger, resolve_tool, run_command
from ..workspace import Workspace
from .audio import probe_duration

log = get_logger(__name__)

STAGE = "transcribe"

FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add "
    "the bin folder to PATH or set tools.ffmpeg in config/settings.json to the "
    "full ffmpeg.exe path."
)

# $/minute, whisper-1. Used only to log an estimate before spending money -
# not billed from here, OpenAI's own usage dashboard is the source of truth.
PRICE_PER_MINUTE = 0.006


def _client(settings: Settings):
    try:
        import openai
    except ImportError as exc:
        raise StageError(
            "The openai package is not installed.\nInstall with: pip install -U openai"
        ) from exc
    key_env = str(settings.get("transcribe.openai.api_key_env", "OPENAI_API_KEY"))
    api_key = os.environ.get(key_env)
    if not api_key:
        raise StageError(
            "{0} is not set.\nSet it in your environment before running with "
            "transcribe.backend: \"openai\".".format(key_env)
        )
    return openai.OpenAI(api_key=api_key)


def _chunk_audio(
    audio_path: Path, scratch_dir: Path, chunk_seconds: int, settings: Settings
) -> List[Path]:
    """Transcode to compressed mono mp3 and split into fixed-length chunks.

    64kbps mono keeps a `chunk_seconds`-default (1200s/20min) chunk at
    roughly 9.6MB - comfortable margin under the API's 25MB cap without
    needing per-chunk size checks.
    """
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    scratch_dir.mkdir(parents=True)

    binary = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    pattern = scratch_dir / "chunk_%03d.mp3"
    run_command(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(audio_path),
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
        ],
        log=log,
    )
    chunks = sorted(scratch_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise StageError("ffmpeg produced no chunks in {0}".format(scratch_dir))
    return chunks


def _transcribe_chunk(
    client, model: str, chunk_path: Path, language: Optional[str], max_retries: int
) -> Any:
    """One chunk, retried with backoff. Raises the last error if every attempt fails."""
    kwargs: Dict[str, Any] = {"model": model, "response_format": "verbose_json"}
    if language:
        kwargs["language"] = language

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            with chunk_path.open("rb") as fh:
                return client.audio.transcriptions.create(file=fh, **kwargs)
        except Exception as exc:  # network/API errors - retry, don't sink the chunk yet
            last_exc = exc
            if attempt < max_retries:
                log.warning(
                    "  %s: attempt %d/%d failed (%s), retrying...",
                    chunk_path.name, attempt, max_retries, exc,
                )
                time.sleep(2 ** attempt)
    raise last_exc  # type: ignore[misc]


def transcribe_audio_openai(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    audio_path: Optional[Path] = None,
    out_path: Optional[Path] = None,
    mark_stage: bool = True,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Transcribe the workspace audio via OpenAI's API. Returns the transcript JSON path."""
    audio = Path(audio_path) if audio_path else ws.audio_path
    target = Path(out_path) if out_path else ws.transcript_path

    if target.exists() and not force:
        log.info("Transcript already exists, skipping: %s", target.name)
        return target

    if not audio.exists():
        raise StageError("No audio at {0}. Run the audio stage first.".format(audio))

    client = _client(settings)
    model = str(settings.get("transcribe.openai.model", "whisper-1"))
    chunk_seconds = max(60, int(settings.get("transcribe.openai.chunk_seconds", 1200)))
    concurrency = max(1, int(settings.get("transcribe.openai.concurrency", 4)))
    max_retries = max(1, int(settings.get("transcribe.openai.max_retries", 3)))
    threshold = float(settings.get("transcribe.low_confidence_threshold", -0.7))
    max_segment_seconds = float(settings.get("transcribe.max_segment_seconds", 30.0))

    language = settings.get("transcribe.language") or None
    if isinstance(language, str) and language.lower() in ("auto", "none", "null", ""):
        language = None

    # duration is the audio-probe value from state.json - never re-derived
    # from kick_duration, per the invariant every other stage follows.
    duration = float(ws.read_state().get("duration") or probe_duration(audio, settings) or 0.0)
    estimated_cost = (duration / 60.0) * PRICE_PER_MINUTE if duration else None
    log.info(
        "Transcribing %s via OpenAI %s (language=%s)%s",
        audio.name,
        model,
        language or "auto-detect",
        " - estimated cost ${0:.2f} ({1:.1f} min @ ${2}/min)".format(
            estimated_cost, duration / 60.0, PRICE_PER_MINUTE
        )
        if estimated_cost is not None
        else "",
    )

    scratch_dir = ws.root / "_transcribe_openai"
    chunks = _chunk_audio(audio, scratch_dir, chunk_seconds, settings)
    log.info(
        "Split into %d chunk(s) of up to %ds, uploading with %d concurrent request(s)",
        len(chunks), chunk_seconds, concurrency,
    )

    progress.phase("transcribe", total=duration or None, unit="s")
    started = time.time()

    def _run_one(index_and_path):
        index, chunk_path = index_and_path
        try:
            response = _transcribe_chunk(client, model, chunk_path, language, max_retries)
            return index, response, None
        except Exception as exc:  # a bad chunk must not sink the whole run
            return index, None, exc

    results: Dict[int, Any] = {}
    errors: Dict[int, Exception] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for index, response, exc in pool.map(_run_one, enumerate(chunks)):
            progress.check_cancelled()
            if exc is not None:
                errors[index] = exc
                log.warning("  chunk %d/%d failed after retries: %s", index + 1, len(chunks), exc)
            else:
                results[index] = response
            completed += 1
            progress.update(min(completed * chunk_seconds, duration or completed * chunk_seconds))

    segments: List[Dict[str, Any]] = []
    low_conf = 0
    for index in range(len(chunks)):
        offset = index * chunk_seconds
        if index in errors:
            segments.append(
                {
                    "id": len(segments),
                    "start": round(offset, 3),
                    "end": round(offset + chunk_seconds, 3),
                    "text": "",
                    "avg_logprob": 0.0,
                    "no_speech_prob": 0.0,
                    "compression_ratio": 0.0,
                    "low_confidence": True,
                    "flags": ["openai_chunk_failed"],
                }
            )
            low_conf += 1
            continue

        response = results[index]
        for seg in getattr(response, "segments", None) or []:
            avg_logprob = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
            no_speech = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
            start = float(getattr(seg, "start", 0.0) or 0.0) + offset
            end = float(getattr(seg, "end", 0.0) or 0.0) + offset
            seg_duration = end - start

            flags = []
            if avg_logprob < threshold:
                flags.append("low_logprob")
            if max_segment_seconds and seg_duration > max_segment_seconds:
                flags.append("implausible_duration")
            is_low = bool(flags)
            if is_low:
                low_conf += 1

            segments.append(
                {
                    "id": len(segments),
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": (getattr(seg, "text", "") or "").strip(),
                    "avg_logprob": round(avg_logprob, 4),
                    "no_speech_prob": round(no_speech, 4),
                    "compression_ratio": round(
                        float(getattr(seg, "compression_ratio", 0.0) or 0.0), 4
                    ),
                    "low_confidence": is_low,
                    "flags": flags,
                }
            )

    elapsed = time.time() - started
    speed = (duration / elapsed) if elapsed and duration else 0.0
    if not errors:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    log.info(
        "Transcribed %d segments in %.1f min (%.2fx realtime); %d flagged low-confidence, "
        "%d/%d chunk(s) failed",
        len(segments), elapsed / 60.0, speed, low_conf, len(errors), len(chunks),
    )
    if errors:
        log.warning(
            "Chunk failures kept their scratch files at %s for debugging.", scratch_dir
        )

    payload = {
        "audio_file": audio.name,
        "model": model,
        "device": "openai-api",
        "compute_type": "n/a",
        "backend": "openai",
        "language": language,
        "language_probability": None,
        "duration": round(duration, 3),
        "segment_count": len(segments),
        "low_confidence_count": low_conf,
        "low_confidence_threshold": threshold,
        "max_segment_seconds": max_segment_seconds,
        "transcribe_seconds": round(elapsed, 1),
        "realtime_factor": round(speed, 3),
        "failed_chunks": len(errors),
        "options": {
            "model": model,
            "chunk_seconds": chunk_seconds,
            "concurrency": concurrency,
            "language": language,
        },
        "segments": segments,
    }
    ws.write_json(target, payload)
    log.info("Wrote %s", target)

    if mark_stage:
        ws.mark_stage(
            STAGE,
            transcript_file=target.name,
            model=model,
            backend="openai",
            segments=len(segments),
            low_confidence=low_conf,
            failed_chunks=len(errors),
            realtime_factor=round(speed, 3),
        )
    return target
