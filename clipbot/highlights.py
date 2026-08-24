"""Text-independent "something happened here" signals for stage 4 (analyze).

The transcript `analyze.py` sends to Claude is 100% text, so a moment that is
pure laughter, a shout, or a sudden reaction - with no distinctive words - is
invisible to the model no matter how the rubric is worded. This module
computes two independent signals from data the pipeline already has, and
hands `analyze.py` a merged, sorted list of "notable moment" spans it
annotates directly onto matching transcript lines (see
`analyze.format_transcript`'s `signals` argument):

  - audio loudness, via `chatsync.audio_energy_envelope` (already proven on
    multi-hour files, O(1) memory - no new way of reading audio.wav)
  - chat message rate, via `stages.chat.messages_between` (the existing
    shared query function, already handling the chat-clock-vs-VOD-clock
    offset correction)

Both use z-score outlier detection against the whole-VOD mean/std for that
signal, not a fixed threshold - a stream's own baseline loudness and chat
activity vary too much (a quiet talking segment vs. an intense game moment)
for one absolute cutoff to mean the same thing throughout.

Degrades gracefully by design: a notable-moments list is a hint, not a hard
dependency, so a missing/unavailable audio or chat source just skips that one
signal (logged, not raised) rather than failing the analyze stage.
"""

import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

from .chatsync import audio_energy_envelope
from .config import Settings
from .stages.chat import messages_between
from .utils import get_logger
from .workspace import Workspace

log = get_logger(__name__)


def _flag_spans(
    values: List[float], bin_seconds: float, z_threshold: float
) -> List[Dict[str, float]]:
    """Merge consecutive bins whose z-score (against the whole series) meets
    `z_threshold` into spans. Returns each span's time range, peak z-score,
    and its own mean value (the caller can turn that into a "Nx normal"
    figure against the series mean if it wants one)."""
    n = len(values)
    if n < 2:
        return []
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if stdev < 1e-9:  # flat series (e.g. total silence) - nothing to flag
        return []

    flagged = [(v - mean) / stdev >= z_threshold for v in values]
    spans = []
    i = 0
    while i < n:
        if not flagged[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flagged[j + 1]:
            j += 1
        span_values = values[i : j + 1]
        peak_z = max((v - mean) / stdev for v in span_values)
        spans.append(
            {
                "start": round(i * bin_seconds, 1),
                "end": round((j + 1) * bin_seconds, 1),
                "z_score": round(peak_z, 2),
                "mean_value": statistics.fmean(span_values),
                "series_mean": mean,
            }
        )
        i = j + 1
    return spans


def compute_energy_spikes(
    audio_path: Path, bin_seconds: float = 5.0, z_threshold: float = 2.0
) -> List[Dict[str, float]]:
    """Loudness outlier spans - the signal for laughter/shouting/loud
    reactions that may have no distinctive transcribed words at all."""
    envelope = audio_energy_envelope(Path(audio_path), bin_seconds)
    spans = _flag_spans(envelope, bin_seconds, z_threshold)
    return [{"start": s["start"], "end": s["end"], "z_score": s["z_score"]} for s in spans]


def compute_chat_spikes(
    chat_doc: Dict[str, Any],
    offset_seconds: float,
    duration: float,
    bin_seconds: float = 15.0,
    z_threshold: float = 2.0,
) -> List[Dict[str, float]]:
    """Chat message-rate outlier spans - a burst of chat activity is often a
    more reliable proxy for "this was funny/notable" than the words said."""
    if not duration or duration <= 0:
        return []
    num_bins = int(duration // bin_seconds) + 1
    counts = [0.0] * num_bins
    for message in messages_between(chat_doc, 0.0, duration, offset=offset_seconds):
        idx = int(message["offset"] // bin_seconds)
        if 0 <= idx < num_bins:
            counts[idx] += 1.0

    out = []
    for span in _flag_spans(counts, bin_seconds, z_threshold):
        multiplier = (
            span["mean_value"] / span["series_mean"] if span["series_mean"] > 1e-9 else 0.0
        )
        out.append(
            {
                "start": span["start"],
                "end": span["end"],
                "z_score": span["z_score"],
                "message_rate_multiplier": round(multiplier, 1),
            }
        )
    return out


def notable_moments(ws: Workspace, settings: Settings) -> List[Dict[str, Any]]:
    """Merged, sorted `[{start, end, label}]` spans for `analyze.py` to
    annotate onto matching transcript lines."""
    if not settings.get("analyze.signals.enabled", True):
        return []

    moments: List[Dict[str, Any]] = []
    state = ws.read_state()
    duration = state.get("duration")

    if ws.audio_path.exists():
        try:
            bin_seconds = float(settings.get("analyze.signals.audio_bin_seconds", 5.0))
            z_threshold = float(settings.get("analyze.signals.audio_z_threshold", 2.0))
            for span in compute_energy_spikes(ws.audio_path, bin_seconds, z_threshold):
                moments.append({"start": span["start"], "end": span["end"], "label": "energy spike"})
        except Exception as exc:  # a broken signal must not sink the analyze stage
            log.warning("Energy-spike detection skipped: %s", exc)
    else:
        log.debug("No audio.wav in %s - skipping energy-spike detection", ws.root)

    if duration and ws.chat_path.exists():
        try:
            chat_doc = ws.read_json(ws.chat_path)
            if chat_doc.get("status") == "ok":
                offset = float(state.get("chat_offset_seconds") or 0.0)
                bin_seconds = float(settings.get("analyze.signals.chat_bin_seconds", 15.0))
                z_threshold = float(settings.get("analyze.signals.chat_z_threshold", 2.0))
                for span in compute_chat_spikes(chat_doc, offset, duration, bin_seconds, z_threshold):
                    moments.append(
                        {
                            "start": span["start"],
                            "end": span["end"],
                            "label": "chat spike, {0:.1f}x".format(span["message_rate_multiplier"]),
                        }
                    )
            else:
                log.debug("chat.json status=%s - skipping chat-spike detection", chat_doc.get("status"))
        except Exception as exc:
            log.warning("Chat-spike detection skipped: %s", exc)
    else:
        log.debug("No chat.json or unknown duration - skipping chat-spike detection")

    moments.sort(key=lambda m: m["start"])
    return moments
