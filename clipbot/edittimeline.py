"""Edit timeline: turn a validated edit spec into concrete times.

Pure math, no IO (like `editspec.py`, `reelspec.py`). It answers every "when" a
renderer or a checker needs:

* how long each clip really is once its speed ramps and freezes are applied, and
  where a source second lands inside it (`pieces` + `src_to_out`);
* where each clip starts on the timeline once transitions overlap;
* where every marker, overlay, camera key, effect and track item falls, in output
  seconds (`anchor` resolution - see `editspec`'s module notes for why a time
  written against another clip must be resolved here, after the timeline exists);
* the *events* a viewer perceives, which the pacing gate counts.

Frame grid
----------
Every clip length is rounded to a whole number of output frames. xfade offsets and
audio crossfades are computed from these lengths, so a fraction of a frame left
over in each of 40 clips would show up as an audible drift by the end; snapping
each clip once, here, is what keeps video and audio the same length at every seam.
"""

import math
from typing import Any, Dict, List, Optional

from . import editspec

# A speed ramp is built from short constant-rate pieces. 0.1 source seconds is six
# frames at 60 fps: fine enough that the change of rate reads as a ramp, coarse
# enough that a 3-second ramp is 30 filter pieces rather than 180.
RAMP_STEP = 0.1

# What the pacing gate counts as "something new on screen".
EVENT_KINDS = ("cut", "transition", "overlay", "camera", "slowmo", "freeze", "fx", "sfx", "marker")


# --------------------------------------------------------------------------
# retiming
# --------------------------------------------------------------------------


def rate_at(keys: List[Dict[str, Any]], t: float) -> float:
    """The speed rate at source second t for a list of speed keys."""
    if not keys:
        return 1.0
    if keys[0]["at"] is None:  # constant
        return keys[0]["rate"]
    if t <= keys[0]["at"]:
        return keys[0]["rate"]
    if t >= keys[-1]["at"]:
        return keys[-1]["rate"]
    for a, b in zip(keys, keys[1:]):
        if a["at"] <= t <= b["at"]:
            p = (t - a["at"]) / (b["at"] - a["at"])
            return a["rate"] + (b["rate"] - a["rate"]) * editspec.ease_value(b["ease"], p)
    return keys[-1]["rate"]


def build_pieces(clip: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The clip as consecutive pieces: constant-rate `play` pieces and `freeze` holds.

    Each piece has `out0` (where it starts inside the clip's output) and `out_dur`.
    A piece with no speed change is one `play` piece covering the whole clip, which
    is what lets the builder skip retiming entirely for the common case.
    """
    a, b = clip["in"], clip["out"]
    keys = clip.get("speed") or []

    # boundaries where the rate can change
    cuts = {a, b}
    if keys and keys[0]["at"] is not None:
        for k in keys:
            if a < k["at"] < b:
                cuts.add(k["at"])
    for f in clip.get("freezes") or []:
        if a < f["at"] < b:
            cuts.add(f["at"])
    bounds = sorted(cuts)

    raw: List[Dict[str, Any]] = []
    for s0, s1 in zip(bounds, bounds[1:]):
        r0, r1 = rate_at(keys, s0), rate_at(keys, s1)
        if keys and keys[0]["at"] is not None and abs(r1 - r0) > 0.02:
            steps = max(2, int(math.ceil((s1 - s0) / RAMP_STEP)))
            edges = [s0 + (s1 - s0) * i / steps for i in range(steps + 1)]
            for e0, e1 in zip(edges, edges[1:]):
                raw.append({"kind": "play", "s0": e0, "s1": e1, "rate": rate_at(keys, (e0 + e1) / 2.0)})
        else:
            raw.append({"kind": "play", "s0": s0, "s1": s1, "rate": rate_at(keys, (s0 + s1) / 2.0)})

    pieces: List[Dict[str, Any]] = []
    holds = {round(f["at"], 6): f["dur"] for f in (clip.get("freezes") or [])}
    for p in raw:
        pieces.append(p)
        end = round(p["s1"], 6)
        if end in holds:
            pieces.append({"kind": "freeze", "s0": p["s1"], "s1": p["s1"], "rate": 0.0, "hold": holds[end]})
    # a freeze exactly at the clip start has no preceding piece
    first_hold = holds.get(round(a, 6))
    if first_hold:
        pieces.insert(0, {"kind": "freeze", "s0": a, "s1": a, "rate": 0.0, "hold": first_hold})

    t = 0.0
    for p in pieces:
        p["out0"] = t
        p["out_dur"] = p["hold"] if p["kind"] == "freeze" else (p["s1"] - p["s0"]) / p["rate"]
        t += p["out_dur"]
    return pieces


def is_plain(pieces: List[Dict[str, Any]]) -> bool:
    """True for a clip played straight through at 1x: no retiming needed at all."""
    return len(pieces) == 1 and pieces[0]["kind"] == "play" and abs(pieces[0]["rate"] - 1.0) < 1e-9


def src_to_out(pieces: List[Dict[str, Any]], t: float) -> float:
    """Where source second t lands inside the clip's output (before the freeze at t)."""
    last = 0.0
    for p in pieces:
        if p["kind"] == "freeze":
            if abs(t - p["s0"]) < 1e-6:
                return p["out0"]
            continue
        if p["s0"] - 1e-9 <= t <= p["s1"] + 1e-9:
            return p["out0"] + (t - p["s0"]) / p["rate"]
        last = p["out0"] + p["out_dur"]
    return last


def _frames(seconds: float, fps: float) -> int:
    return max(1, int(round(seconds * fps)))


# --------------------------------------------------------------------------
# compile
# --------------------------------------------------------------------------


def compile_timeline(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a normalized spec into the timeline the renderer and checks use.

    Returns:
      {
        "fps", "duration", "profile",
        "clips": [{id, index, type, start, dur, end, frames, pieces, plain,
                   overlap_in, transition_in, markers, overlays, camera, fx,
                   grade_window, warnings}],
        "tracks": {"music": [...], "sfx": [...]},   # absolute timeline seconds
        "events": [{t, kind, label}],               # sorted
        "warnings": [str],
      }
    """
    fps = float(spec["output"]["fps"])
    warnings: List[str] = []
    clips: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}

    t_cursor = 0.0
    prev: Optional[Dict[str, Any]] = None
    for index, c in enumerate(spec["clips"]):
        entry: Dict[str, Any] = {"id": c["id"], "index": index, "type": c["type"], "label": c["label"],
                                 "tags": c["tags"], "warnings": []}
        if c["type"] == "card":
            dur = c["dur"]
            entry.update({"pieces": [], "plain": True})
        else:
            pieces = build_pieces(c)
            dur = sum(p["out_dur"] for p in pieces)
            entry.update({"pieces": pieces, "plain": is_plain(pieces), "src": c["src"],
                          "in": c["in"], "out": c["out"]})
        frames = _frames(dur, fps)
        entry["frames"] = frames
        entry["dur"] = frames / fps

        ti = c["transition_in"]
        overlap = 0.0
        if prev is not None and ti["type"] == "xfade":
            # an xfade eats into both clips; keep the overlap inside either one
            overlap = min(ti["dur"], prev["dur"] * 0.5, entry["dur"] * 0.5)
            overlap = max(1, int(round(overlap * fps))) / fps
        entry["transition_in"] = dict(ti)
        entry["overlap_in"] = overlap
        entry["start"] = max(0.0, t_cursor - overlap) if prev is not None else 0.0
        entry["end"] = entry["start"] + entry["dur"]
        t_cursor = entry["end"]

        _localize(c, entry)
        clips.append(entry)
        by_id[c["id"]] = entry
        prev = entry

    duration = clips[-1]["end"]
    tracks = _resolve_tracks(spec, by_id, duration, warnings)
    for e in clips:
        warnings.extend("{0}: {1}".format(e["id"], w) for w in e["warnings"])

    events = _events(spec, clips, tracks)
    return {
        "fps": fps, "duration": duration, "profile": spec["profile"],
        "clips": clips, "tracks": tracks, "events": events, "warnings": warnings,
    }


def _localize(c: Dict[str, Any], entry: Dict[str, Any]) -> None:
    """Map a clip's source-second positions to clip-local output seconds."""
    pieces, dur = entry["pieces"], entry["dur"]

    def local(t: float) -> float:
        return src_to_out(pieces, t) if c["type"] == "clip" else t

    entry["markers"] = []
    for m in c["markers"]:
        t = local(m["at"])
        entry["markers"].append({"name": m["name"], "at": t})

    entry["overlays"] = []
    for i, o in enumerate(c["overlays"]):
        t0 = local(o["at"]) if c["type"] == "clip" else o["at"]
        t1 = min(dur, t0 + o["dur"])
        if t0 >= dur - 0.05:
            entry["warnings"].append("overlay {0} starts after the clip ends and was dropped".format(o.get("id", i)))
            continue
        d = dict(o)
        d["t0"], d["t1"] = max(0.0, t0), t1
        d["index"] = i
        entry["overlays"].append(d)

    entry["camera"] = [dict(k, t=local(k["at"])) for k in c.get("camera", [])] if c["type"] == "clip" else []
    entry["fx"] = []
    if c["type"] == "clip":
        for e in c["fx"]:
            e = dict(e)
            e["at"] = round(local(e["at"]), 3)
            entry["fx"].append(e)
        g = c.get("grade")
        entry["grade_window"] = [local(g["window"][0]), local(g["window"][1])] if g and g.get("window") else None
    else:
        entry["grade_window"] = None


def _anchor_time(anchor: Dict[str, Any], by_id: Dict[str, Dict[str, Any]], warnings: List[str], what: str) -> Optional[float]:
    e = by_id[anchor["clip"]]
    if "marker" in anchor:
        marks = [m for m in e["markers"] if m["name"] == anchor["marker"]]
        if not marks:
            warnings.append("{0}: clip {1!r} has no marker named {2!r}".format(what, e["id"], anchor["marker"]))
            return None
        t = marks[0]["at"]
    elif "at" in anchor:
        t = src_to_out(e["pieces"], anchor["at"]) if e["type"] == "clip" else anchor["at"]
    elif anchor.get("from") == "end":
        t = e["dur"]
    else:
        t = 0.0
    return e["start"] + t + anchor["offset"]


def _resolve_tracks(spec, by_id, duration, warnings):
    music = []
    for m in spec["tracks"]["music"]:
        start = 0.0 if m["start"] == "start" else _anchor_time(m["start"], by_id, warnings, "music " + m["id"])
        end = duration if m["end"] == "end" else _anchor_time(m["end"], by_id, warnings, "music " + m["id"])
        if start is None or end is None:
            continue
        start, end = max(0.0, start), min(duration, end)
        if end - start < 0.5:
            warnings.append("music {0}: runs for {1:.2f} s and was dropped".format(m["id"], end - start))
            continue
        music.append(dict(m, t0=start, t1=end))
    sfx = []
    for s in spec["tracks"]["sfx"]:
        hit = _anchor_time(s["on"], by_id, warnings, "sfx " + s["id"])
        if hit is None:
            continue
        if hit < 0 or hit > duration:
            warnings.append("sfx {0}: lands at {1:.2f} s, outside the video, and was dropped".format(s["id"], hit))
            continue
        sfx.append(dict(s, hit=hit))
    sfx.sort(key=lambda s: s["hit"])
    return {"music": music, "sfx": sfx}


def _events(spec, clips, tracks) -> List[Dict[str, Any]]:
    ev: List[Dict[str, Any]] = []

    def add(t, kind, label):
        ev.append({"t": round(t, 3), "kind": kind, "label": label})

    for e in clips:
        if e["index"] > 0:
            add(e["start"], "cut", e["id"])
            if e["transition_in"]["type"] != "cut":
                add(e["start"], "transition", "{0}:{1}".format(e["id"], e["transition_in"]["type"]))
        elif e["type"] == "card":
            add(e["start"], "overlay", e["id"] + ":card")
        for o in e["overlays"]:
            add(e["start"] + o["t0"], "overlay", "{0}:{1}".format(e["id"], o.get("id", o["kind"])))
        for k in e["camera"]:
            add(e["start"] + k["t"], "camera", "{0}:zoom{1}".format(e["id"], k["zoom"]))
        for p in e["pieces"]:
            if p["kind"] == "freeze":
                add(e["start"] + p["out0"], "freeze", e["id"])
        keys = (spec["clips"][e["index"]].get("speed") or []) if e["type"] == "clip" else []
        if keys and keys[0]["at"] is not None:
            for k0, k1 in zip(keys, keys[1:]):
                if abs(k1["rate"] - k0["rate"]) >= 0.15:
                    add(e["start"] + src_to_out(e["pieces"], k0["at"]), "slowmo" if k1["rate"] < k0["rate"] else "camera",
                        "{0}:rate{1}".format(e["id"], k1["rate"]))
        for f in e["fx"]:
            add(e["start"] + f["at"], "fx", "{0}:{1}".format(e["id"], f["type"]))
        for m in e["markers"]:
            add(e["start"] + m["at"], "marker", "{0}:{1}".format(e["id"], m["name"]))
    for s in tracks["sfx"]:
        add(s["hit"], "sfx", s["id"])
    ev.sort(key=lambda x: x["t"])
    return ev


# --------------------------------------------------------------------------
# the pacing gate
# --------------------------------------------------------------------------

DWELL_MIN = 1.0   # a callout shown for less than this reads as a flicker
DWELL_MAX = 4.0   # past this it is a hold and the picture should have moved on


def pacing(timeline: Dict[str, Any], max_gap: Optional[float] = None) -> Dict[str, Any]:
    """Audit the timeline for stretches with nothing new on screen.

    Borrowed from the beats gate in github.com/ranahaani/i-hate-editing: walk the
    sorted events and flag any gap longer than the profile allows, check that
    something happens in the opening two seconds, and bound how long an overlay
    stays up. A plain gameplay clip has no events of its own, so a reel of raw
    clips fails this on purpose - the cure is a speed ramp, a zoom, a callout or a
    sound on the moment that matters, which is what makes an edit feel edited.
    """
    profile = editspec.PROFILES[timeline["profile"]]
    gap_limit = float(max_gap if max_gap is not None else profile["max_gap"])
    dur = timeline["duration"]
    times = [0.0] + [e["t"] for e in timeline["events"] if e["kind"] != "marker"] + [dur]
    times = sorted(set(round(t, 3) for t in times))
    dead: List[Dict[str, Any]] = []
    for a, b in zip(times, times[1:]):
        if b - a > gap_limit + 1e-6:
            dead.append({"start": a, "end": b, "length": round(b - a, 2)})

    issues: List[Dict[str, Any]] = []
    first = next((e["t"] for e in timeline["events"] if e["kind"] not in ("marker", "cut") and e["t"] < dur), None)
    if first is None or first > 2.0:
        issues.append({"severity": "warn", "what": "opening",
                       "detail": "nothing new happens in the first 2 s (first event at {0})".format(
                           "none" if first is None else "{0:.1f} s".format(first))})
    for e in timeline["clips"]:
        for o in e["overlays"]:
            shown = o["t1"] - o["t0"]
            if shown < DWELL_MIN - 1e-6:
                issues.append({"severity": "warn", "what": "dwell",
                               "detail": "{0} overlay {1} is up for {2:.2f} s (< {3} s, reads as a flicker)".format(
                                   e["id"], o.get("id", o["index"]), shown, DWELL_MIN)})
            elif shown > DWELL_MAX + 1e-6:
                issues.append({"severity": "warn", "what": "dwell",
                               "detail": "{0} overlay {1} is up for {2:.1f} s (> {3} s)".format(
                                   e["id"], o.get("id", o["index"]), shown, DWELL_MAX)})
    for d in dead:
        issues.append({"severity": "warn", "what": "dead", "detail": "{0} s with nothing new, {1:.1f}-{2:.1f} s".format(
            d["length"], d["start"], d["end"])})

    sfx_ceiling = max(3, int(profile["sfx_per_15s"] * dur / 15.0))
    if len(timeline["tracks"]["sfx"]) > sfx_ceiling:
        issues.append({"severity": "warn", "what": "sfx-density",
                       "detail": "{0} sound effects in {1:.0f} s; the {2} profile tops out near {3}".format(
                           len(timeline["tracks"]["sfx"]), dur, timeline["profile"], sfx_ceiling)})

    return {"max_gap": gap_limit, "events": len(timeline["events"]), "duration": round(dur, 2),
            "dead": dead, "issues": issues, "ok": not issues}


def chapters(timeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    """YouTube-style chapters: clips tagged `chapter` (or a card), at least 10 s apart;
    one that would start sooner is folded into the previous chapter's title.

    YouTube only turns the list into chapters when it starts at 0:00, has three or
    more entries and every one is at least 10 s long; this returns what the edit
    can offer and leaves it to `editdeliver` to say whether it qualifies.
    """
    out: List[Dict[str, Any]] = []
    for e in timeline["clips"]:
        if "chapter" in e["tags"] or (e["type"] == "card" and "nochapter" not in e["tags"]):
            if out and e["start"] - out[-1]["t"] < 10.0:
                # too close to stand alone: fold it into the chapter before it, by name,
                # rather than dropping it - a 3 s intro card then "Headshots" is one
                # chapter called "Intro + Headshots", not a chapter list that never
                # mentions the headshots
                if e["label"] not in out[-1]["title"].split(" + "):
                    out[-1]["title"] += " + " + e["label"]
                continue
            out.append({"t": round(e["start"], 2), "title": e["label"]})
    if out and out[0]["t"] > 0.01:
        out.insert(0, {"t": 0.0, "title": "Intro"})
    return out
