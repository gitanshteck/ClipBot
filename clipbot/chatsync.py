"""Estimate `chat_offset_seconds` by correlating chat activity against the VOD's
own audio energy.

The correction this solves for: chat message offsets are stored relative to
`stream_started_at` (when Kick opened the livestream record), but a clip's
`start`/`end` are VOD-time (relative to the recording's own frame 0). Those two
clocks agree only if recording began the instant the stream was created, which
it measurably does not - on this channel, by about 26.6s. See `chatsync.py`'s
`estimate_offset` docstring for the sign convention, which matches
`stages.chat.messages_between`.

Deliberately reads `audio.wav` directly rather than depending on
`waveform.json`: that property exists on `Workspace` but nothing in this
codebase currently generates it (checked - `waveform` is a listed job kind with
no registered handler). A coarse per-second RMS envelope is all correlation
needs, and `audio.wav` is already guaranteed mono 16-bit PCM
(`config/settings.json` -> `audio.*`), which the stdlib `wave`/`audioop` modules
read natively - no new dependency, no separate generation step to keep in sync.
"""

import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import StageError, get_logger

log = get_logger(__name__)

try:
    import audioop  # stdlib; removed in Python 3.13, fine while this targets 3.9
except ImportError:  # pragma: no cover - only hit on 3.13+
    audioop = None


def audio_energy_envelope(audio_path: Path, bin_seconds: float) -> List[float]:
    """Per-bin RMS loudness, streamed rather than loaded whole (a 4.6h stream is
    ~500MB of raw PCM - reading it in ~16000-frame chunks keeps this O(1) memory)."""
    if audioop is None:
        raise StageError(
            "The stdlib 'audioop' module isn't available on this Python "
            "(removed in 3.13+). Chat sync estimation needs it."
        )
    with wave.open(str(audio_path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise StageError(
                "chat sync expects 16-bit PCM audio.wav, got sampwidth={0}. Re-run "
                "the audio stage with the default codec.".format(wf.getsampwidth())
            )
        channels = wf.getnchannels()
        rate = wf.getframerate()
        frames_per_bin = max(1, int(round(rate * bin_seconds)))
        envelope: List[float] = []
        while True:
            raw = wf.readframes(frames_per_bin)
            if not raw:
                break
            if channels > 1:
                raw = audioop.tomono(raw, 2, 0.5, 0.5)
            envelope.append(float(audioop.rms(raw, 2)))
    return envelope


def _histogram(messages: List[Dict[str, Any]], bin_seconds: float) -> Dict[int, int]:
    """Message count per bin, keyed by raw (uncalibrated) stored offset.

    Sparse on purpose - on this channel roughly half of all 45s windows have no
    messages at all, so a dense array over the full harvested window (which
    includes the +/-margin_seconds padding) would be mostly zeros.
    """
    hist: Dict[int, int] = {}
    for message in messages:
        idx = int(message["offset"] // bin_seconds)
        hist[idx] = hist.get(idx, 0) + 1
    return hist


def _correlate(
    audio_env: List[float], hist: Dict[int, int], lag_lo: int, lag_hi: int, span: Tuple[int, int]
) -> List[Tuple[int, float]]:
    """score(lag) = sum over i in span of (audio[i]-mean_a) * (hist[i+lag]-mean_c).

    `lag` is a bin-index shift; a positive lag tests whether the chat histogram,
    shifted backward, better matches audio energy - i.e. whether recording
    started `lag * bin_seconds` after the chat clock's zero point.
    """
    i0, i1 = span
    n = i1 - i0
    if n <= 0:
        return []
    mean_a = sum(audio_env[i0:i1]) / n
    mean_c = sum(hist.values()) / max(1, len(audio_env)) if hist else 0.0

    scores = []
    for lag in range(lag_lo, lag_hi + 1):
        total = 0.0
        for i in range(i0, i1):
            c = hist.get(i + lag, 0) - mean_c
            total += (audio_env[i] - mean_a) * c
        scores.append((lag, total))
    return scores


def estimate_offset(
    ws,
    chat_doc: Dict[str, Any],
    settings=None,
) -> Dict[str, Any]:
    """Cross-correlate chat activity against audio energy across the whole VOD.

    Returns a diagnostics dict; does not write anything. Caller decides whether
    to persist `offset_seconds` into `state.json`.
    """
    audio_path = ws.audio_path
    if not audio_path.exists():
        raise StageError(
            "No audio.wav in this workspace - run the audio stage before "
            "estimating a chat sync offset."
        )
    messages = chat_doc.get("messages") or []
    if len(messages) < 20:
        raise StageError(
            "Only {0} chat message(s) harvested - too few to correlate "
            "reliably. Set chat_offset_seconds by eye instead.".format(len(messages))
        )

    def cfg(key, default):
        return settings.get("chat." + key, default) if settings is not None else default

    bin_seconds = float(cfg("sync_bin_seconds", 5.0))
    lo, hi = cfg("sync_search_range_seconds", [-60, 300])
    lag_lo, lag_hi = int(round(lo / bin_seconds)), int(round(hi / bin_seconds))

    log.info(
        "Estimating chat offset: %d messages, %.0fs bins, searching %+.0f..%+.0fs",
        len(messages), bin_seconds, lo, hi,
    )
    audio_env = audio_energy_envelope(audio_path, bin_seconds)
    n = len(audio_env)
    if n < 10:
        raise StageError("Audio track too short to correlate ({0} bin(s)).".format(n))

    hist = _histogram(messages, bin_seconds)

    full_scores = _correlate(audio_env, hist, lag_lo, lag_hi, (0, n))
    if not full_scores:
        raise StageError("Nothing to correlate - the VOD has no audio bins.")
    best_lag, best_score = max(full_scores, key=lambda pair: pair[1])
    values = [s for _, s in full_scores]
    mean_score = sum(values) / len(values)
    variance = sum((v - mean_score) ** 2 for v in values) / len(values)
    std = variance ** 0.5
    # How much the true peak stands out from correlation noise. A flat, wide
    # peak (low z-score) means the audio/chat relationship is weak - long
    # silences, very sparse chat - and the estimate should be treated as rough.
    z_score = (best_score - mean_score) / std if std > 1e-9 else 0.0

    # Quarter-split: if a mid-stream reconnect dropped recording time without
    # pausing chat, the true offset is not constant, and a single global lag
    # will be visibly wrong in one half. Each quarter is correlated against the
    # SAME histogram (chat's own clock never resets) but only its own audio span.
    quarters = []
    step = n // 4
    for q in range(4):
        i0 = q * step
        i1 = n if q == 3 else (q + 1) * step
        if i1 - i0 < 5:
            continue
        scores = _correlate(audio_env, hist, lag_lo, lag_hi, (i0, i1))
        if not scores:
            continue
        q_lag, _ = max(scores, key=lambda pair: pair[1])
        quarters.append({
            "start_seconds": round(i0 * bin_seconds, 1),
            "end_seconds": round(i1 * bin_seconds, 1),
            "offset_seconds": round(q_lag * bin_seconds, 1),
        })

    nonlinear_warning = None
    if len(quarters) >= 2:
        spread = max(q["offset_seconds"] for q in quarters) - min(
            q["offset_seconds"] for q in quarters
        )
        # A few bins of jitter is normal quantisation noise, not a real gap.
        if spread > 3 * bin_seconds:
            nonlinear_warning = (
                "Per-quarter offsets disagree by {0:.0f}s, which usually means a "
                "mid-stream reconnect or drop - a single workspace-wide offset "
                "won't fit every clip. Consider a per-clip chat.offset override "
                "for clips on the far side of the gap.".format(spread)
            )

    # Secondary anchor for exactly the case correlation handles badly: a stream
    # sparse enough that there's too little signal for the dot product to find
    # a sharp peak (measured on this channel: 358 messages over 4.6h scores
    # z=2.2, below the confidence gate). A streamer signing off and chat saying
    # bye is common enough that "last message vs. the VOD's own end" is often a
    # tighter estimate than statistical correlation can manage on thin data -
    # not a replacement for it, a cross-check.
    duration = ws.read_state().get("duration")
    boundary_offset = None
    if duration:
        boundary_offset = round(messages[-1]["offset"] - float(duration), 1)

    result = {
        "offset_seconds": round(best_lag * bin_seconds, 1),
        "score": best_score,
        "z_score": round(z_score, 2),
        "confident": z_score >= 3.0,
        "boundary_offset_seconds": boundary_offset,
        "bin_seconds": bin_seconds,
        "search_range_seconds": [lo, hi],
        "message_count": len(messages),
        "quarters": quarters,
        "warning": nonlinear_warning,
    }
    log.info(
        "  correlation estimate: %+.1fs (z=%.2f%s)",
        result["offset_seconds"], z_score,
        "" if result["confident"] else ", LOW CONFIDENCE",
    )
    if boundary_offset is not None:
        log.info(
            "  boundary estimate (last message vs. VOD end): %+.1fs%s",
            boundary_offset,
            "  <- more likely correct here, correlation is unconfident"
            if not result["confident"] else "",
        )
    if nonlinear_warning:
        log.warning("  %s", nonlinear_warning)
    return result
