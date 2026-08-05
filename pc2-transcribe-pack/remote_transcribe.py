"""Standalone transcription script for the GPU machine.

Does NOT import the clipbot package - it's a deliberate duplicate of
clipbot/stages/transcribe.py's model options, low-confidence flagging logic,
and transcript.json schema, kept intentionally small so this machine only
needs `pip install faster-whisper` instead of the whole ClipBot dependency
stack (ffmpeg, yt-dlp, FastAPI, Anthropic SDK, ...).

If you ever change transcribe.py's thresholds/options in the main repo,
mirror the change here too - there's no shared import to keep them in sync
automatically.

Usage:
    python remote_transcribe.py --audio audio.wav --out transcript.json
    python remote_transcribe.py --audio audio.wav --out transcript.json --device cuda --language hi

Output transcript.json has the exact schema clipbot/stages/transcribe.py
produces, so it drops straight into work/<slug>/transcript.json on the main
PC with no conversion step.
"""

import argparse
import json
import time
from pathlib import Path


def resolve_device(requested: str) -> str:
    if requested and requested != "auto":
        return requested
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def resolve_compute_type(requested: str, device: str) -> str:
    if requested and requested != "default":
        return requested
    return "float16" if device == "cuda" else "int8"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path, help="Path to audio.wav")
    parser.add_argument("--out", required=True, type=Path, help="Path to write transcript.json")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="auto", help="auto | cuda | cpu")
    parser.add_argument("--compute-type", default="default")
    parser.add_argument("--cpu-threads", type=int, default=0)
    parser.add_argument("--language", default="hi", help="Language code, or 'auto' to detect")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--no-vad", action="store_true", help="Disable VAD filter")
    parser.add_argument("--condition-on-previous-text", action="store_true")
    parser.add_argument("--no-speech-threshold", type=float, default=0.6)
    parser.add_argument("--compression-ratio-threshold", type=float, default=2.4)
    parser.add_argument("--log-prob-threshold", type=float, default=-1.0)
    parser.add_argument("--initial-prompt", default=None)
    parser.add_argument("--low-confidence-threshold", type=float, default=-0.7)
    parser.add_argument("--max-segment-seconds", type=float, default=30.0)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="Only transcribe the first N seconds (quick benchmark/smoke test)",
    )
    args = parser.parse_args()

    if not args.audio.exists():
        raise SystemExit("Audio file not found: {0}".format(args.audio))

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SystemExit(
            "faster-whisper is not installed.\nInstall with: pip install -U faster-whisper"
        ) from exc

    device = resolve_device(args.device)
    compute_type = resolve_compute_type(args.compute_type, device)

    if device == "cpu":
        print(
            "WARNING: running on CPU ({0}). If this machine has an NVIDIA GPU, "
            "check nvidia-smi and that ctranslate2 sees it (see setup.ps1).".format(compute_type)
        )

    print("Loading {0} (device={1}, compute_type={2})...".format(args.model, device, compute_type))
    started = time.time()
    kwargs = {"device": device, "compute_type": compute_type}
    if args.cpu_threads:
        kwargs["cpu_threads"] = args.cpu_threads
    model = WhisperModel(args.model, **kwargs)
    print("Model ready in {0:.1f}s".format(time.time() - started))

    language = args.language
    if isinstance(language, str) and language.lower() in ("auto", "none", "null", ""):
        language = None

    options = {
        "language": language,
        "beam_size": args.beam_size,
        "vad_filter": not args.no_vad,
        "condition_on_previous_text": args.condition_on_previous_text,
        "no_speech_threshold": args.no_speech_threshold,
        "compression_ratio_threshold": args.compression_ratio_threshold,
        "log_prob_threshold": args.log_prob_threshold,
    }
    if args.initial_prompt:
        options["initial_prompt"] = args.initial_prompt
    if args.max_seconds:
        options["clip_timestamps"] = [0.0, args.max_seconds]

    print(
        "Transcribing {0} (language={1}, beam_size={2}, vad={3})".format(
            args.audio.name, language or "auto-detect", args.beam_size, not args.no_vad
        )
    )
    started = time.time()
    segment_iter, info = model.transcribe(str(args.audio), **options)

    total = float(getattr(info, "duration", 0.0) or 0.0)
    detected = getattr(info, "language", None)
    lang_prob = float(getattr(info, "language_probability", 0.0) or 0.0)
    print(
        "Detected language: {0} (p={1:.2f}), audio duration {2:.1f} min".format(
            detected, lang_prob, total / 60.0 if total else 0.0
        )
    )

    segments = []
    low_conf = 0
    last_log = 0.0
    threshold = args.low_confidence_threshold
    max_segment_seconds = args.max_segment_seconds

    for seg in segment_iter:
        avg_logprob = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
        no_speech = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
        duration = float(seg.end) - float(seg.start)

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
                "compression_ratio": round(float(getattr(seg, "compression_ratio", 0.0) or 0.0), 4),
                "low_confidence": is_low,
                "flags": flags,
            }
        )

        if total and seg.end - last_log >= 300:
            last_log = seg.end
            elapsed = time.time() - started
            pct = 100.0 * seg.end / total
            speed = seg.end / elapsed if elapsed else 0.0
            eta = (total - seg.end) / speed if speed else 0.0
            print(
                "  {0:.0f}% ({1:.0f}/{2:.0f} min audio) - {3:.2f}x realtime, ETA {4:.0f} min".format(
                    pct, seg.end / 60.0, total / 60.0, speed, eta / 60.0
                )
            )

    elapsed = time.time() - started
    if not segments:
        print("WARNING: no speech segments found in {0}".format(args.audio.name))

    speed = (total / elapsed) if elapsed and total else 0.0
    print(
        "Transcribed {0} segments in {1:.1f} min ({2:.2f}x realtime); {3} flagged low-confidence".format(
            len(segments), elapsed / 60.0, speed, low_conf
        )
    )

    payload = {
        "audio_file": args.audio.name,
        "model": args.model,
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
        "options": options,
        "segments": segments,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(args.out)
    print("Wrote {0}".format(args.out))


if __name__ == "__main__":
    main()
