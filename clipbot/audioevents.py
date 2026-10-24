"""The reaction map: what the audio of a stream contains, second by second (pure functions, numpy only).

`stages/events.py` runs the PANNs tagger (`panns.py`) over a workspace's `audio.wav` and hands the per-second class
probabilities to this module, which reduces them to a handful of *categories* the editor cares about - laughter,
shouting, gunfire, explosions, cheering, music, speech, silence - and finds their peaks. The result is
`events.json`. Why categories and not the 527 classes: a lineup pick needs to say "he laughs here", not
"Chuckle, chortle 0.31, Giggle 0.22", and a multi-hour stream's full probability matrix is 60 MB of mostly zeros.

A category's score for a second is the highest probability among its AudioSet classes (see `panns.CATEGORIES`),
stored as an integer 0-100. What a score *means* was measured, not assumed - see the docstring of `stages/events.py`.
"""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import panns

# Where a category counts as "present". Measured on this channel's audio (stages/events.py); a probability is not a
# calibrated chance, so these are cut points, not promises.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "speech": 0.5, "laughter": 0.25, "shout": 0.25, "gunfire": 0.25, "explosion": 0.25, "cheer": 0.25,
    "music": 0.5, "silence": 0.5,
}
MERGE_GAP = 2          # seconds between two runs above the threshold that are still one peak
REACTION = ("laughter", "shout")


def union_classes() -> List[int]:
    """Every class index any category uses, sorted: the columns the tagger is asked for."""
    return sorted({i for idx in panns.CATEGORIES.values() for i in idx})


def category_series(probs: np.ndarray, classes: Sequence[int]) -> Dict[str, np.ndarray]:
    """`probs` is (seconds, len(classes)) for the columns `classes`; returns one (seconds,) array per category."""
    col = {c: i for i, c in enumerate(classes)}
    out: Dict[str, np.ndarray] = {}
    for name, idx in panns.CATEGORIES.items():
        out[name] = probs[:, [col[i] for i in idx]].max(axis=1) if len(probs) else np.zeros(0, dtype=np.float32)
    return out


def quantize(series: np.ndarray) -> List[int]:
    return [int(v) for v in np.clip(np.rint(series * 100.0), 0, 100)]


def find_peaks(scores: Sequence[float], threshold: float, merge_gap: int = MERGE_GAP, offset: int = 0) -> List[Dict[str, Any]]:
    """Runs of seconds at or above `threshold`, runs with at most `merge_gap` quiet seconds between them joined. Each peak is
    `{start, end, t, p, mean}`: `start`/`end` in seconds on the stream's clock (end exclusive), `t` the second of
    the highest score and `p` that score (0-1)."""
    s = np.asarray(scores, dtype=np.float32)
    hot = np.flatnonzero(s >= threshold)
    peaks: List[Dict[str, Any]] = []
    if not len(hot):
        return peaks
    start = prev = int(hot[0])
    runs: List[Tuple[int, int]] = []
    for i in hot[1:]:
        i = int(i)
        if i - prev - 1 > merge_gap:                  # more than `merge_gap` quiet seconds between: a new peak
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    for a, b in runs:
        seg = s[a:b + 1]
        k = int(seg.argmax())
        peaks.append({"start": offset + a, "end": offset + b + 1, "t": offset + a + k, "p": round(float(seg[k]), 2),
                      "mean": round(float(seg.mean()), 2)})
    return peaks


def build_document(workspace: str, duration: float, series: Dict[str, np.ndarray], thresholds: Optional[Dict[str, float]] = None,
                   first: int = 0, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The `events.json` document. `first` is the stream second the series starts at (a partial scan)."""
    th = dict(DEFAULT_THRESHOLDS)
    th.update(thresholds or {})
    doc: Dict[str, Any] = {
        "schema": 1, "workspace": workspace, "model": {"name": "Cnn14_16k", "md5": panns.CHECKPOINT_MD5},
        "window": panns.WINDOW_SECONDS, "duration": duration, "first": first,
        "seconds": int(len(next(iter(series.values())))) if series else 0,
        "thresholds": th,
        "categories": {name: quantize(arr) for name, arr in series.items()},
        "peaks": {name: find_peaks(arr, th[name], offset=first) for name, arr in series.items()},
    }
    if extra:
        doc.update(extra)
    return doc


def score_at(doc: Dict[str, Any], category: str, t: float) -> float:
    """The category's score (0-1) at stream second `t` (0 outside what was scanned)."""
    arr = doc["categories"].get(category)
    i = int(t) - int(doc.get("first", 0))
    if arr is None or i < 0 or i >= len(arr):
        return 0.0
    return arr[i] / 100.0


def peak_between(doc: Dict[str, Any], category: str, a: float, b: float) -> Tuple[float, Optional[float]]:
    """`(score, second)` of the highest score of `category` within [a, b): `(0.0, None)` when none was scanned."""
    arr = doc["categories"].get(category)
    if not arr:
        return 0.0, None
    first = int(doc.get("first", 0))
    lo = max(0, int(a) - first)
    hi = min(len(arr), int(np.ceil(b)) - first)
    if hi <= lo:
        return 0.0, None
    seg = arr[lo:hi]
    k = int(np.argmax(seg))
    return seg[k] / 100.0, float(first + lo + k)


def reaction_between(doc: Dict[str, Any], a: float, b: float) -> Dict[str, Any]:
    """The strongest reaction sound (laughter or shouting) inside [a, b): `{category, p, t}` or `{p: 0.0}`."""
    best: Dict[str, Any] = {"p": 0.0}
    for cat in REACTION:
        p, t = peak_between(doc, cat, a, b)
        if p > best["p"]:
            best = {"category": cat, "p": round(p, 2), "t": t}
    return best


def top_peaks(doc: Dict[str, Any], category: str, n: int = 20, min_p: float = 0.0, min_gap: float = 20.0) -> List[Dict[str, Any]]:
    """The `n` strongest peaks of a category, strongest first, no two closer than `min_gap` seconds (a long laugh is
    one peak, not five)."""
    chosen: List[Dict[str, Any]] = []
    for pk in sorted(doc.get("peaks", {}).get(category, []), key=lambda p: (-p["p"], -p["mean"], p["t"])):
        if pk["p"] < min_p:
            continue
        if all(abs(pk["t"] - c["t"]) >= min_gap for c in chosen):
            chosen.append(pk)
        if len(chosen) >= n:
            break
    return chosen


# --------------------------------------------------------------------------
# checking the map against something that happened (`clipbot events check`)
# --------------------------------------------------------------------------

# What the audience types when something is funny. A noisy label - chat also laughs at chat - but it is independent of
# the audio, which is the point of using it.
CHAT_LAUGH = re.compile(r"(kekw|\blul\b|lulw|omegalul|😂|🤣|\blmao|\blmfao|\bhaha|\bhahaha|💀)", re.I)


def auc(positive: Sequence[float], negative: Sequence[float]) -> float:
    """P(a positive scores above a negative), ties counting half (the Mann-Whitney statistic): 0.5 is chance, 1.0 is
    a score that always ranks the positives first. NaN when either side is empty."""
    pos = np.asarray(positive, dtype=float)
    neg = np.asarray(negative, dtype=float)
    if not len(pos) or not len(neg):
        return float("nan")
    allv = np.concatenate([pos, neg])
    ranks = allv.argsort().argsort() + 1.0
    for v in np.unique(allv):
        idx = allv == v
        if idx.sum() > 1:
            ranks[idx] = ranks[idx].mean()
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


def window_max(series: Sequence[float], a: float, b: float) -> float:
    """The highest value of a per-second series over seconds [a, b] (inclusive; 0 outside it)."""
    s = np.asarray(series, dtype=float)
    lo, hi = max(0, int(a)), min(len(s), int(b) + 1)
    return float(s[lo:hi].max()) if hi > lo else 0.0


def chat_laugh_times(chat_doc: Dict[str, Any], offset: float = 0.0) -> List[float]:
    """VOD seconds of the chat messages that laugh (`offset` is `state.chat_offset_seconds`)."""
    if chat_doc.get("status") != "ok":
        return []
    return sorted(m["offset"] - offset for m in chat_doc.get("messages", []) if CHAT_LAUGH.search(m.get("text", "")))


def check_series(series: Dict[str, Sequence[float]], times: Sequence[float], before: float, after: float, seed: int = 1,
                 samples: int = 3000, hit_at: float = 0.25) -> Dict[str, Dict[str, float]]:
    """For each named per-second series: how well its highest value in `[t - before, t + after]` separates the given
    moments `times` from random moments of the same stream. `{name: {auc, at_moments, at_random}}`, the last two being
    the share of moments (and of random moments) where the series reached `hit_at`."""
    rng = np.random.RandomState(seed)
    n = min(len(v) for v in series.values())
    out: Dict[str, Dict[str, float]] = {}
    rand = rng.randint(int(before) + 5, max(int(before) + 6, n - int(after) - 5), size=samples)
    for name, ser in series.items():
        pos = [window_max(ser, t - before, t + after) for t in times if 0 <= t < n]
        neg = [window_max(ser, t - before, t + after) for t in rand]
        out[name] = {"auc": round(auc(pos, neg), 2), "at_moments": round(float(np.mean(np.array(pos) >= hit_at)), 2) if pos else float("nan"),
                     "at_random": round(float(np.mean(np.array(neg) >= hit_at)), 2)}
    return out


def top_hit_rate(peak_times: Sequence[float], moments: Sequence[float], after: float = 12.0) -> Tuple[int, int]:
    """`(hits, listed)`: how many of the listed peaks are followed within `after` seconds by one of `moments`."""
    m = np.asarray(moments, dtype=float)
    hits = sum(1 for t in peak_times if len(m) and bool(((m >= t) & (m <= t + after)).any()))
    return hits, len(peak_times)


def random_hit_rate(seconds: int, k: int, moments: Sequence[float], after: float = 12.0, trials: int = 200, seed: int = 0) -> float:
    """The share of `k` random moments of a `seconds`-long stream followed within `after` seconds by one of `moments`
    (the average of `trials` draws): what `top_hit_rate` has to beat."""
    if k <= 0 or seconds < 50:
        return 0.0
    rng = np.random.RandomState(seed)
    return float(np.mean([top_hit_rate(list(rng.randint(20, seconds - 20, size=k)), moments, after)[0] / float(k) for _ in range(trials)]))
