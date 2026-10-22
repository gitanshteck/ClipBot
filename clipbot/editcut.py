"""Pause-aware cut points: land a cut in a gap in the talking, not in a word.

A transcript timestamp is good to about two seconds, which is the wrong resolution
for a joke: cut a beat early and the punchline is gone, a beat late and the next
sentence starts. The workspace's own `audio.wav` (16 kHz mono, the clock every
other timestamp in the project lives on) has the answer: speech over a game has
short dips between phrases, and a cut belongs in one.

`find_cut` looks for a dip within `window` seconds of where the author wanted the
cut and moves the cut into it -

* an *in* point goes just before the speech that follows the gap (a quarter of a
  second of room, so the first word is not clipped);
* an *out* point goes just after the speech that came before it (0.3 s, enough to
  carry the breath or the end of a laugh).

A dip is a stretch of at least `min_len` seconds in the bottom of the surrounding
seconds' own loudness range (`find_pauses`). Over a firefight there is no such dip, and the honest answer is "unsnapped": the time is left exactly where the
author put it and the caller is told, never moved on a guess.

numpy and the stdlib `wave` module only; the signal maths is plain arrays so it is
unit-tested on synthetic audio.
"""

import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import StageError

HOP = 0.01              # envelope resolution, seconds
WINDOW = 0.06           # RMS window, seconds
CONTEXT = 3.5           # seconds of audio read either side of the target
IN_ROOM = 0.25          # an in point lands this long before the speech resumes
OUT_TAIL = 0.30         # an out point lands this long after the speech stops


def read_envelope(wav_path: Any, a: float, b: float) -> Tuple[Any, Any]:
    """(times, level_db) of `audio.wav` between a and b seconds, one value per HOP."""
    import numpy as np

    try:
        with wave.open(str(wav_path), "rb") as w:
            rate, frames, channels, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
            if width != 2:
                raise StageError("{0}: expected 16-bit PCM, found {1}-bit".format(wav_path, width * 8))
            i0 = max(0, int(a * rate))
            i1 = min(frames, int(b * rate))
            if i1 - i0 < int(WINDOW * rate) * 2:
                return np.zeros(0), np.zeros(0)
            w.setpos(i0)
            raw = w.readframes(i1 - i0)
    except (wave.Error, EOFError, OSError) as exc:
        raise StageError("could not read {0}: {1}".format(wav_path, exc))
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return envelope(x, rate, i0 / float(rate))


def envelope(samples: Any, rate: int, t0: float = 0.0) -> Tuple[Any, Any]:
    """RMS level in dB, one value per HOP, each over a WINDOW; times are window centres."""
    import numpy as np

    hop, win = max(1, int(rate * HOP)), max(2, int(rate * WINDOW))
    if len(samples) < win * 2:
        return np.zeros(0), np.zeros(0)
    csum = np.concatenate([[0.0], np.cumsum(np.asarray(samples, dtype=np.float64) ** 2)])
    starts = np.arange(0, len(samples) - win, hop)
    rms = np.sqrt((csum[starts + win] - csum[starts]) / win)
    return t0 + (starts + win / 2.0) / float(rate), 20.0 * np.log10(np.maximum(rms, 1e-5))


def find_pauses(times: Any, level_db: Any, floor_frac: float = 0.3, min_range_db: float = 12.0,
                min_len: float = 0.2) -> List[Dict[str, float]]:
    """Gaps in the talking: runs of at least `min_len` seconds in the bottom `floor_frac`
    of the stretch's own dynamic range (between its 10th and 90th percentile level).

    A stretch whose loud and quiet parts are less than `min_range_db` apart is steady
    noise or music with nothing to cut into, and has no pauses. The range is measured on
    the stretch itself, not against its median: most of a talking stretch *is* quiet,
    which drags a median down below the floor. [{start, end, depth}], earliest first;
    `depth` is how far under the loud level the run sinks.
    """
    import numpy as np

    if len(times) == 0:
        return []
    lo, hi = float(np.percentile(level_db, 10)), float(np.percentile(level_db, 90))
    if hi - lo < min_range_db:
        return []
    quiet = level_db <= lo + (hi - lo) * floor_frac
    out: List[Dict[str, float]] = []
    i, n = 0, len(quiet)
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and quiet[j + 1]:
            j += 1
        start, end = float(times[i]), float(times[j])
        if end - start >= min_len:
            out.append({"start": round(start, 3), "end": round(end, 3),
                        "depth": round(hi - float(np.min(level_db[i:j + 1])), 1)})
        i = j + 1
    return out


def choose(pauses: List[Dict[str, float]], t: float, kind: str, window: float) -> Optional[Dict[str, float]]:
    """The pause nearest `t` (within `window`), judged by where the cut would land in it."""
    best, best_d = None, None
    for p in pauses:
        at = cut_point(p, kind)
        d = abs(at - t)
        if d <= window and (best_d is None or d < best_d):
            best, best_d = p, d
    return best


def cut_point(pause: Dict[str, float], kind: str) -> float:
    """Where in a pause a cut lands: just before the speech that follows it (`in`), or a
    breath after the speech that came before it (`out`). A pause shorter than the room
    asked for is cut in its middle."""
    middle = pause["start"] + (pause["end"] - pause["start"]) / 2.0
    if kind == "in":
        return max(middle, pause["end"] - IN_ROOM)
    return min(middle, pause["start"] + OUT_TAIL)


def find_cut(wav_path: Any, t: float, kind: str, window: float = 0.8, floor_frac: float = 0.3,
             min_len: float = 0.2) -> Dict[str, Any]:
    """Move a cut at `t` into the nearest gap in the speech.

    `kind` is `in` or `out`. Returns {t, snapped, moved, pause, depth}; when no gap is
    within `window` seconds, `t` is returned unchanged with `snapped` False.
    """
    if kind not in ("in", "out"):
        raise ValueError("kind must be 'in' or 'out'")
    times, db = read_envelope(wav_path, t - CONTEXT, t + CONTEXT)
    pause = choose(find_pauses(times, db, floor_frac, min_len=min_len), t, kind, window)
    if pause is None:
        return {"t": round(t, 3), "snapped": False, "moved": 0.0, "pause": None, "depth": None}
    at = round(cut_point(pause, kind), 3)
    return {"t": at, "snapped": True, "moved": round(at - t, 3), "pause": [pause["start"], pause["end"]],
            "depth": pause["depth"]}


# --------------------------------------------------------------------------
# level matching and whole-lineup preparation
# --------------------------------------------------------------------------

MATCH_STRENGTH = 0.85   # how much of a clip's distance from the typical level is corrected
MATCH_LIMIT_DB = 8.0    # ...and the most any clip is moved


def clip_level(wav_path: Any, a: float, b: float) -> Optional[float]:
    """How loud a clip sounds while something is going on in it, in dB: the mean of its
    loudest 40% of frames. A mean over everything would call a clip with long pauses
    quiet; the peak would call a single shot loud."""
    import numpy as np

    times, db = read_envelope(wav_path, a, b)
    if len(db) < 20:
        return None
    top = np.sort(db)[int(len(db) * 0.6):]
    return float(np.mean(top))


def _lineup_clips(lineup: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cold = lineup.get("cold_open")
    if cold:
        out += cold.get("clips", [])
    for chapter in lineup.get("chapters", []):
        out += chapter.get("clips", [])
    return out


def prepare_lineup(lineup: Dict[str, Any], wav_for: Any, snap: bool = True, match: bool = True,
                   window: float = 0.8) -> Dict[str, Any]:
    """Snap every clip's cut points into pauses and match the clips' levels, in place.

    `wav_for(alias)` returns the `audio.wav` of a clip's source, or None when it has none
    (the clip is then left as authored). A clip with `kills` keeps its window - its
    callouts hang off kill times inside it - and a clip with `"snap": false` is left
    alone. A clip that already has `gain_db` keeps it. Returns a report:
    {clips: [{src, in, out, in_was, out_was, snapped_in, snapped_out, level, gain_db}], unsnapped, moved}.
    """
    rows: List[Dict[str, Any]] = []
    for c in _lineup_clips(lineup):
        wav = wav_for(c["src"])
        row: Dict[str, Any] = {"src": c["src"], "in_was": float(c["in"]), "out_was": float(c["out"]),
                               "snapped_in": False, "snapped_out": False, "level": None, "gain_db": None,
                               "note": c.get("note", "")}
        if wav is not None and snap and not c.get("kills") and c.get("snap", True):
            new_in = find_cut(wav, float(c["in"]), "in", window)
            new_out = find_cut(wav, float(c["out"]), "out", window)
            if new_out["t"] - new_in["t"] >= 1.0:           # never snap a clip down to nothing
                c["in"], c["out"] = new_in["t"], new_out["t"]
                row["snapped_in"], row["snapped_out"] = new_in["snapped"], new_out["snapped"]
        row["in"], row["out"] = float(c["in"]), float(c["out"])
        if wav is not None and match:
            row["level"] = clip_level(wav, float(c["in"]), float(c["out"]))
        row["_clip"] = c
        rows.append(row)
    levels = sorted(r["level"] for r in rows if r["level"] is not None)
    if match and levels:
        target = levels[len(levels) // 2]
        for r in rows:
            c = r["_clip"]
            if r["level"] is None or "gain_db" in c:
                continue
            gain = max(-MATCH_LIMIT_DB, min(MATCH_LIMIT_DB, (target - r["level"]) * MATCH_STRENGTH))
            c["gain_db"] = round(gain * 2.0) / 2.0              # half-dB steps: a number a person can read
            r["gain_db"] = c["gain_db"]
    for r in rows:
        del r["_clip"]
    return {"clips": rows, "unsnapped": sum(1 for r in rows for k in ("snapped_in", "snapped_out") if not r[k]),
            "moved": sum(1 for r in rows if r["in"] != r["in_was"] or r["out"] != r["out_was"])}
