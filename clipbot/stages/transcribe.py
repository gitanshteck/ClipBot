"""Stage 3: transcribe the extracted audio, locally or via OpenAI's API.

Produces segment-level timestamps in `transcript.json`. `transcribe_audio()`
is a thin dispatcher: `transcribe.backend` (default `"openai"`) or a
per-call `backend` override picks between this module's local
faster-whisper path and `transcribe_openai.py`'s hosted-API path. This
module's own docstring used to invite exactly this swap ("the contract
downstream stages rely on is the JSON schema, nothing more") - the local
path below is that contract's original, still-default-capable
implementation, not the only one anymore.

Hindi/English code-switching is the hard case here: the model handles it
unevenly, and the usual failure is a segment coming back with plausible-looking
but wrong text rather than an obvious error. Low-confidence segments are
therefore **flagged and kept**, never dropped — the analysis stage is told to
treat them as approximate.

transcript.json:
    {
      "audio_file": "audio.wav",
      "model": "large-v3",
      "language": "hi",
      "language_probability": 0.98,
      "duration": 4402.07,
      "segment_count": 812,
      "low_confidence_count": 47,
      "options": {...},
      "segments": [
        {
          "id": 0,
          "start": 12.34,
          "end": 15.02,
          "text": "...",
          "avg_logprob": -0.31,
          "no_speech_prob": 0.02,
          "compression_ratio": 1.4,
          "low_confidence": false
        }
      ]
    }
"""

import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import Settings
from ..progress import NULL_PROGRESS, Progress
from ..utils import StageError, get_logger
from ..workspace import Workspace

log = get_logger(__name__)

STAGE = "transcribe"


def _resolve_device(requested: str) -> str:
    """Pick a device. CTranslate2 is CUDA-only — there is no ROCm/AMD path."""
    if requested and requested != "auto":
        return requested
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception as exc:  # pragma: no cover - depends on local install
        log.debug("CUDA probe failed: %s", exc)
    return "cpu"


def _resolve_compute_type(requested: str, device: str) -> str:
    """int8 is the sane CPU default; it's several times faster at little cost."""
    if requested and requested != "default":
        return requested
    return "float16" if device == "cuda" else "int8"


def _load_model(settings: Settings):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise StageError(
            "faster-whisper is not installed.\n"
            "Install with: pip install -U faster-whisper"
        ) from exc

    model_name = str(settings.get("transcribe.model", "large-v3"))
    device = _resolve_device(str(settings.get("transcribe.device", "auto")))
    compute_type = _resolve_compute_type(
        str(settings.get("transcribe.compute_type", "default")), device
    )

    if device == "cpu":
        log.warning(
            "Running %s on CPU (%s) - CTranslate2 has no AMD/ROCm backend, so a "
            "Radeon GPU can't be used here. Expect this to take a while.",
            model_name,
            compute_type,
        )

    log.info("Loading %s (device=%s, compute_type=%s)", model_name, device, compute_type)
    started = time.time()
    kwargs: Dict[str, Any] = {"device": device, "compute_type": compute_type}
    cpu_threads = settings.get("transcribe.cpu_threads")
    if cpu_threads:
        kwargs["cpu_threads"] = int(cpu_threads)

    model = WhisperModel(model_name, **kwargs)
    log.info("Model ready in %.1fs", time.time() - started)
    return model, model_name, device, compute_type


def transcribe_audio(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    audio_path: Optional[Path] = None,
    out_path: Optional[Path] = None,
    mark_stage: bool = True,
    progress: Progress = NULL_PROGRESS,
    backend: Optional[str] = None,
) -> Path:
    """Transcribe the workspace audio. Returns the transcript JSON path.

    `backend` is a per-call override ("local" or "openai"); `None` (the
    default) falls through to the `transcribe.backend` setting. This is the
    same override-vs-settings-default relationship `force` already has
    relative to a caller's own default.
    """
    backend = str(backend or settings.get("transcribe.backend", "openai"))
    if backend == "openai":
        from .transcribe_openai import transcribe_audio_openai

        return transcribe_audio_openai(
            ws,
            settings,
            force=force,
            audio_path=audio_path,
            out_path=out_path,
            mark_stage=mark_stage,
            progress=progress,
        )

    audio = Path(audio_path) if audio_path else ws.audio_path
    target = Path(out_path) if out_path else ws.transcript_path

    if target.exists() and not force:
        log.info("Transcript already exists, skipping: %s", target.name)
        return target

    if not audio.exists():
        raise StageError(
            "No audio at {0}. Run the audio stage first.".format(audio)
        )

    model, model_name, device, compute_type = _load_model(settings)

    language = settings.get("transcribe.language") or None
    if isinstance(language, str) and language.lower() in ("auto", "none", "null", ""):
        language = None  # env overrides arrive as strings; treat these as auto-detect
    beam_size = int(settings.get("transcribe.beam_size", 5))
    vad_filter = bool(settings.get("transcribe.vad_filter", True))
    threshold = float(settings.get("transcribe.low_confidence_threshold", -0.7))
    max_segment_seconds = float(settings.get("transcribe.max_segment_seconds", 30.0))

    options: Dict[str, Any] = {
        "language": language,
        "beam_size": beam_size,
        "vad_filter": vad_filter,
        # Whisper loops on filler and music; these are the standard guards.
        "condition_on_previous_text": bool(
            settings.get("transcribe.condition_on_previous_text", False)
        ),
        # Hallucination guards. Whisper invents text over music and silence -
        # a real problem here, since streams have background music playing.
        "no_speech_threshold": float(
            settings.get("transcribe.no_speech_threshold", 0.6)
        ),
        "compression_ratio_threshold": float(
            settings.get("transcribe.compression_ratio_threshold", 2.4)
        ),
        "log_prob_threshold": float(
            settings.get("transcribe.log_prob_threshold", -1.0)
        ),
    }
    initial_prompt = settings.get("transcribe.initial_prompt")
    if initial_prompt:
        options["initial_prompt"] = str(initial_prompt)

    log.info(
        "Transcribing %s (language=%s, beam_size=%d, vad=%s)",
        audio.name,
        language or "auto-detect",
        beam_size,
        vad_filter,
    )
    started = time.time()
    segment_iter, info = model.transcribe(str(audio), **options)

    total = float(getattr(info, "duration", 0.0) or 0.0)
    detected = getattr(info, "language", None)
    lang_prob = float(getattr(info, "language_probability", 0.0) or 0.0)
    log.info(
        "Detected language: %s (p=%.2f), audio duration %.1f min",
        detected,
        lang_prob,
        total / 60.0 if total else 0.0,
    )

    segments = []
    low_conf = 0
    last_log = 0.0
    progress.phase("transcribe", total=total or None, unit="s")

    # faster-whisper yields lazily; the work happens as we iterate.
    for seg in segment_iter:
        avg_logprob = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
        no_speech = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
        duration = float(seg.end) - float(seg.start)

        # Two independent signals, both calibrated against real stream audio:
        #  - avg_logprob separates cleanly at about -0.7 on this content;
        #    hallucinated windows sat at -0.72..-0.90, good speech at -0.23..-0.58.
        #  - a "segment" running tens of seconds is not one utterance, it's the
        #    decoder having lost the plot over music or silence.
        flags = []
        if avg_logprob < threshold:
            flags.append("low_logprob")
        if max_segment_seconds and duration > max_segment_seconds:
            flags.append("implausible_duration")
        is_low = bool(flags)
        if is_low:
            low_conf += 1

        segments.append(
            {
                "id": len(segments),
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": (seg.text or "").strip(),
                "avg_logprob": round(avg_logprob, 4),
                "no_speech_prob": round(no_speech, 4),
                "compression_ratio": round(
                    float(getattr(seg, "compression_ratio", 0.0) or 0.0), 4
                ),
                "low_confidence": is_low,
                "flags": flags,
            }
        )

        # Per-segment updates for the UI (throttled inside Progress), and a
        # cancellation point so a 5-hour job can be stopped within seconds
        # rather than running to completion.
        elapsed_so_far = time.time() - started
        progress.update(
            seg.end,
            total or None,
            label="{0:.2f}x realtime".format(seg.end / elapsed_so_far)
            if elapsed_so_far > 0
            else None,
        )
        progress.check_cancelled()

        if total and seg.end - last_log >= 300:  # progress every ~5 audio-minutes
            last_log = seg.end
            elapsed = time.time() - started
            pct = 100.0 * seg.end / total
            speed = seg.end / elapsed if elapsed else 0.0
            eta = (total - seg.end) / speed if speed else 0.0
            log.info(
                "  %.0f%% (%.0f/%.0f min audio) - %.2fx realtime, ETA %.0f min",
                pct,
                seg.end / 60.0,
                total / 60.0,
                speed,
                eta / 60.0,
            )

    elapsed = time.time() - started
    if not segments:
        log.warning("No speech segments found in %s", audio.name)

    speed = (total / elapsed) if elapsed and total else 0.0
    log.info(
        "Transcribed %d segments in %.1f min (%.2fx realtime); %d flagged low-confidence",
        len(segments),
        elapsed / 60.0,
        speed,
        low_conf,
    )
    if low_conf:
        log.warning(
            "%d/%d segments (%.0f%%) are low-confidence - expected with Hindi/English "
            "code-switching. They are kept and flagged, not dropped.",
            low_conf,
            len(segments),
            100.0 * low_conf / len(segments),
        )

    payload = {
        "audio_file": audio.name,
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "language": detected,
        "language_probability": round(lang_prob, 4),
        "duration": round(total, 3),
        "segment_count": len(segments),
        "low_confidence_count": low_conf,
        "low_confidence_threshold": threshold,
        "max_segment_seconds": max_segment_seconds,
        "transcribe_seconds": round(elapsed, 1),
        "realtime_factor": round(speed, 3),
        "options": {k: v for k, v in options.items()},
        "segments": segments,
    }
    ws.write_json(target, payload)
    log.info("Wrote %s", target)

    if mark_stage:
        ws.mark_stage(
            STAGE,
            transcript_file=target.name,
            model=model_name,
            backend="local",
            device=device,
            segments=len(segments),
            low_confidence=low_conf,
            realtime_factor=round(speed, 3),
        )
    return target
