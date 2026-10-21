"""Audio analysis for the edit engine: sound-effect peaks, beats, clicks at the joins.

numpy only (already a dependency of the dashboard's waveform and the chat sync).
Decoding goes through ffmpeg so any container or codec works; everything after
that is plain array maths and therefore unit-testable on synthetic signals.

Why these three:

* **Peak alignment.** A sound effect's loudest instant - not its first sample - is
  what the ear locks to the picture. A whoosh with its peak 0.4 s in must start
  0.4 s *before* the hit. `find_peak` measures that offset once per file; the
  renderer then starts the sound at `hit - lead - peak`. (Idea from
  github.com/ranahaani/i-hate-editing, MIT.)
* **Beats.** For a montage cut to music: a tempo estimate and a beat grid, so cut
  points and slow-motion landings can be nudged onto the beat.
* **Clicks.** Whether a join between two clips pops. The 30 ms audio fade should
  prevent it; this measures whether it did.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .utils import StageError

ANALYSIS_RATE = 16000


def decode_mono(ffmpeg: str, path: Any, sample_rate: int = ANALYSIS_RATE, start: float = 0.0,
                duration: Optional[float] = None):
    """Decode any media file's first audio stream to a mono float32 numpy array."""
    import numpy as np

    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start:
        argv += ["-ss", "{0:.3f}".format(start)]
    if duration:
        argv += ["-t", "{0:.3f}".format(duration)]
    argv += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise StageError("could not decode audio from {0}: {1}".format(path, proc.stderr.decode("utf-8", "replace")[-300:]))
    return np.frombuffer(proc.stdout, dtype=np.float32)


def find_peak(samples, sample_rate: int = ANALYSIS_RATE, window: float = 0.005) -> Dict[str, float]:
    """The loudest instant of a signal: {peak_at, peak_db, duration}.

    Loudness is measured on a short RMS window (5 ms) rather than on the single
    largest sample, so one stray click does not pull the 'peak' away from the hit
    the ear actually hears.
    """
    import numpy as np

    n = len(samples)
    if n == 0:
        return {"peak_at": 0.0, "peak_db": -120.0, "duration": 0.0}
    w = max(1, int(sample_rate * window))
    sq = samples.astype(np.float64) ** 2
    csum = np.concatenate([[0.0], np.cumsum(sq)])
    idx = np.arange(0, max(1, n - w + 1))
    rms = np.sqrt((csum[idx + w] - csum[idx]) / w)
    i = int(np.argmax(rms))
    peak = float(np.max(np.abs(samples)))
    return {
        "peak_at": float((i + w / 2.0) / sample_rate),
        "peak_db": float(20.0 * np.log10(max(peak, 1e-9))),
        "duration": float(n / float(sample_rate)),
    }


def peak_offset(ffmpeg: str, path: Any, cache_file: Optional[Path] = None) -> Dict[str, float]:
    """`find_peak` for a file, cached by (path, size, mtime) so a library of 200
    effects is analysed once."""
    p = Path(path)
    st = p.stat()
    key = "{0}|{1}|{2}".format(p, st.st_size, int(st.st_mtime))
    cache: Dict[str, Any] = {}
    if cache_file and Path(cache_file).exists():
        try:
            cache = json.loads(Path(cache_file).read_text(encoding="utf-8"))
        except ValueError:
            cache = {}
    if key in cache:
        return cache[key]
    result = find_peak(decode_mono(ffmpeg, p))
    if cache_file:
        cache[key] = result
        try:
            Path(cache_file).parent.mkdir(parents=True, exist_ok=True)
            Path(cache_file).write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return result


def sfx_start(hit_at: float, peak_at: float, lead: float, align: str = "peak") -> Tuple[float, float]:
    """(start, trimmed): where a sound must begin so its peak lands `lead` s before
    the picture event. A start before 0 is clamped and the clipped amount reported."""
    start = hit_at - lead - (peak_at if align == "peak" else 0.0)
    if start < 0:
        return 0.0, -start
    return start, 0.0


# --------------------------------------------------------------------------
# beats
# --------------------------------------------------------------------------


def onset_strength(samples, sample_rate: int, hop: int = 256, frame: int = 1024):
    """Spectral-flux onset envelope: (envelope, frames_per_second)."""
    import numpy as np

    if len(samples) < frame * 2:
        return np.zeros(1), sample_rate / float(hop)
    window = np.hanning(frame).astype(np.float32)
    n_frames = 1 + (len(samples) - frame) // hop
    prev = None
    flux = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        seg = samples[i * hop:i * hop + frame] * window
        mag = np.abs(np.fft.rfft(seg))
        mag = np.log1p(10.0 * mag)  # compress: a quiet hi-hat counts as much as a loud kick
        if prev is not None:
            d = mag - prev
            flux[i] = float(np.sum(d[d > 0]))
        prev = mag
    # remove the slowly varying floor so only real onsets stand out
    k = max(3, int(0.3 * sample_rate / hop))
    floor = np.convolve(flux, np.ones(k) / k, mode="same")
    return np.maximum(flux - floor, 0.0), sample_rate / float(hop)


def estimate_tempo(env, fps: float, lo: float = 70.0, hi: float = 180.0) -> Optional[float]:
    """Beats per minute from the autocorrelation of the onset envelope, or None."""
    import numpy as np

    if len(env) < int(fps * 4) or float(np.max(env)) <= 0:
        return None
    x = env - float(np.mean(env))
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    lag_lo, lag_hi = int(fps * 60.0 / hi), int(fps * 60.0 / lo)
    if lag_hi >= len(ac):
        return None
    lags = np.arange(lag_lo, lag_hi + 1)
    # weight toward ~120 BPM so a half- or double-time peak does not win by accident
    bpm = 60.0 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)
    best = int(lags[int(np.argmax(ac[lags] * weight))])
    return 60.0 * fps / best


def detect_beats(samples, sample_rate: int = ANALYSIS_RATE) -> Dict[str, Any]:
    """{bpm, beats[]}: a beat grid laid on the strongest onsets.

    Finds the tempo, then picks the grid phase whose beats collect the most onset
    energy, and returns the grid across the whole file. A track with no steady
    pulse returns bpm None and no beats - callers should then simply not snap.
    """
    import numpy as np

    env, fps = onset_strength(samples, sample_rate)
    bpm = estimate_tempo(env, fps)
    if not bpm:
        return {"bpm": None, "beats": []}
    period = fps * 60.0 / bpm
    n = len(env)
    best_phase, best_score = 0.0, -1.0
    for phase in np.arange(0.0, period, 0.5):
        idx = np.round(np.arange(phase, n, period)).astype(int)
        idx = idx[idx < n]
        score = float(np.sum(env[idx]))
        if score > best_score:
            best_score, best_phase = score, float(phase)
    idx = np.arange(best_phase, n, period)
    return {"bpm": round(float(bpm), 1), "beats": [round(float(i / fps), 3) for i in idx]}


def snap_to_beat(t: float, beats: Sequence[float], tolerance: float = 0.12) -> float:
    """The nearest beat within `tolerance` seconds of t, else t unchanged."""
    if not beats:
        return t
    nearest = min(beats, key=lambda b: abs(b - t))
    return nearest if abs(nearest - t) <= tolerance else t


# --------------------------------------------------------------------------
# joins
# --------------------------------------------------------------------------


def click_score(samples, sample_rate: int, t: float, half_window: float = 0.015) -> float:
    """How click-like the audio is at time t: the largest sample-to-sample jump in
    +/-15 ms, relative to the signal's own RMS around it.

    Ordinary programme material scores a few; a hard cut through a waveform that
    was not at a zero crossing scores 15+. The ratio is what makes it comparable
    across a quiet clip and a loud one.
    """
    import numpy as np

    i = int(round(t * sample_rate))
    w = max(2, int(half_window * sample_rate))
    seg = samples[max(0, i - w):i + w]
    if len(seg) < 4:
        return 0.0
    jump = float(np.max(np.abs(np.diff(seg))))
    ctx = samples[max(0, i - 8 * w):i + 8 * w]
    rms = float(np.sqrt(np.mean(ctx.astype(np.float64) ** 2)))
    return jump / max(rms, 1e-4)
