"""What changed between two renders of an edit, in words (pure functions, no IO).

A changelog is only useful if someone can read it: "c3_14: in 405.55 -> 405.20 s (clip 0.35 s longer)" is a line a person
can check against what they watched, "clips[14].in changed" is not. `diff_specs` compares two normalised specs (the
`<name>.final.spec.json` record of what a render used) clip by clip, by id, so reordering does not read as everything
having changed, and returns both the structure (who was added, removed, changed) and the flat list of lines.
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_LINES = 60
MOVE = 0.02                 # a trim smaller than this (seconds) is rounding, not an edit

Spec = Dict[str, Any]


def _clip_map(spec: Spec) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in spec.get("clips", [])}


def _s(x: float) -> str:
    return "{0:.2f}".format(float(x)).rstrip("0").rstrip(".")


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _overlay_texts(clip: Dict[str, Any]) -> List[str]:
    out = []
    for o in clip.get("overlays", []):
        if o.get("kind") == "motion":          # a graphic is named by what it shows: "Contact 68", "OpTitle BREACH"
            props = o.get("props") or {}
            out.append("{0} {1}".format(o.get("comp", "motion"), props.get("distance") or props.get("title") or props.get("series") or "").strip())
        else:
            out.append(str(o.get("text", o.get("kind", ""))))
    return out


def _clip_changes(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if old.get("type") == "clip" and new.get("type") == "clip":
        if old.get("src") != new.get("src"):
            out.append("source {0} -> {1}".format(old.get("src"), new.get("src")))
        d_in = float(new["in"]) - float(old["in"])
        d_out = float(new["out"]) - float(old["out"])
        if abs(d_in) > MOVE or abs(d_out) > MOVE:
            parts = []
            if abs(d_in) > MOVE:
                parts.append("in {0} -> {1} s".format(_s(old["in"]), _s(new["in"])))
            if abs(d_out) > MOVE:
                parts.append("out {0} -> {1} s".format(_s(old["out"]), _s(new["out"])))
            grown = d_out - d_in
            parts.append("clip {0} s {1}".format(_s(abs(grown)), "longer" if grown > 0 else "shorter") if abs(grown) > MOVE else "moved")
            out.append(", ".join(parts))
        if not _same(old.get("speed"), new.get("speed")) or not _same(old.get("freezes"), new.get("freezes")):
            out.append("speed or freeze changed")
        if not _same(old.get("camera"), new.get("camera")):
            out.append("camera moves changed ({0} -> {1} keys)".format(len(old.get("camera") or []), len(new.get("camera") or [])))
        if not _same(old.get("grade"), new.get("grade")):
            out.append("grade changed")
        if not _same(old.get("fx"), new.get("fx")):
            out.append("effects changed ({0} -> {1})".format(len(old.get("fx") or []), len(new.get("fx") or [])))
        if not _same(old.get("markers"), new.get("markers")):
            out.append("markers changed ({0} -> {1})".format(len(old.get("markers") or []), len(new.get("markers") or [])))
        if old.get("captions") != new.get("captions"):
            out.append("captions {0} -> {1}".format(_flag(old.get("captions")), _flag(new.get("captions"))))
        if not _same(old.get("framing"), new.get("framing")):
            out.append("framing changed")
    if old.get("type") == "card" and new.get("type") == "card":
        if abs(float(new.get("dur", 0)) - float(old.get("dur", 0))) > MOVE:
            out.append("card length {0} -> {1} s".format(_s(old["dur"]), _s(new["dur"])))
        if old.get("bg") != new.get("bg"):
            out.append("card colour {0} -> {1}".format(old.get("bg"), new.get("bg")))
    if old.get("type") != new.get("type"):
        out.append("changed from a {0} to a {1}".format(old.get("type"), new.get("type")))
    a, b = _overlay_texts(old), _overlay_texts(new)
    if a != b:
        out.append("text {0!r} -> {1!r}".format(" / ".join(a), " / ".join(b)) if a and b else "text {0}".format("added: " + " / ".join(b) if b else "removed: " + " / ".join(a)))
    elif not _same(old.get("overlays"), new.get("overlays")):
        out.append("text moved or restyled")
    ga = float((old.get("audio") or {}).get("gain_db", 0.0))
    gb = float((new.get("audio") or {}).get("gain_db", 0.0))
    if abs(ga - gb) > 0.05:
        out.append("gain {0} -> {1} dB".format(_s(ga), _s(gb)))
    if bool((old.get("audio") or {}).get("mute")) != bool((new.get("audio") or {}).get("mute")):
        out.append("audio {0}".format("muted" if (new.get("audio") or {}).get("mute") else "unmuted"))
    if not _same(old.get("transition_in"), new.get("transition_in")):
        out.append("transition in: {0} -> {1}".format((old.get("transition_in") or {}).get("type"), (new.get("transition_in") or {}).get("type")))
    if old.get("label") != new.get("label"):
        out.append("label {0!r} -> {1!r}".format(old.get("label"), new.get("label")))
    return out


def _flag(v: Any) -> str:
    return "default" if v is None else ("on" if v else "off")


def _global_changes(old: Spec, new: Spec) -> List[str]:
    out: List[str] = []
    if old.get("profile") != new.get("profile"):
        out.append("profile {0} -> {1}".format(old.get("profile"), new.get("profile")))
    oo, no = old.get("output", {}), new.get("output", {})
    if (oo.get("width"), oo.get("height"), oo.get("fps")) != (no.get("width"), no.get("height"), no.get("fps")):
        out.append("output {0}x{1}@{2} -> {3}x{4}@{5}".format(oo.get("width"), oo.get("height"), _s(oo.get("fps") or 0),
                                                              no.get("width"), no.get("height"), _s(no.get("fps") or 0)))
    om, nm = old.get("master", {}), new.get("master", {})
    for k in sorted(set(om) | set(nm)):
        if om.get(k) != nm.get(k):
            out.append("master {0}: {1} -> {2}".format(k, om.get(k), nm.get(k)))
    oc, nc = old.get("captions"), new.get("captions")
    if oc != nc:
        if not oc or not nc:
            out.append("captions {0}".format("turned on" if nc else "turned off"))
        else:
            for k in sorted(set(oc) | set(nc)):
                if oc.get(k) != nc.get(k):
                    out.append("captions {0}: {1} -> {2}".format(k, oc.get(k), nc.get(k)))
    ot, nt = old.get("tracks", {}), new.get("tracks", {})
    for kind in ("music", "sfx"):
        a = {t.get("id"): t for t in ot.get(kind, [])}
        b = {t.get("id"): t for t in nt.get(kind, [])}
        added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
        changed = [i for i in sorted(set(a) & set(b)) if not _same(a[i], b[i])]
        if added:
            out.append("{0} added: {1}".format(kind, ", ".join(str(i) for i in added[:6]) + (" ..." if len(added) > 6 else "")))
        if removed:
            out.append("{0} removed: {1}".format(kind, ", ".join(str(i) for i in removed[:6]) + (" ..." if len(removed) > 6 else "")))
        if changed:
            out.append("{0} changed: {1}".format(kind, ", ".join(str(i) for i in changed[:6]) + (" ..." if len(changed) > 6 else "")))
    return out


def _moved(before: Sequence[str], after: Sequence[str]) -> List[str]:
    """The clips that changed place, as few as possible: those outside the longest run that kept its relative order
    (so moving one clip five places is one moved clip, not five)."""
    pos = {i: k for k, i in enumerate(before)}
    seq = [pos[i] for i in after]
    n = len(seq)
    if n == 0:
        return []
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    k = max(range(n), key=lambda x: best[x])
    keep = set()
    while k != -1:
        keep.add(k)
        k = prev[k]
    return [after[i] for i in range(n) if i not in keep]


def diff_specs(old: Optional[Spec], new: Spec) -> Dict[str, Any]:
    """`{added, removed, moved, changed: {clip id: [lines]}, global: [lines], lines: [str], first: bool}`.

    `old` None is a first render: nothing to compare, and `first` says so. `lines` is the flat, readable list (capped
    at `MAX_LINES` with a count of the rest) that goes into the changelog."""
    if old is None:
        n = len(new.get("clips", []))
        return {"added": [c["id"] for c in new.get("clips", [])], "removed": [], "moved": [], "changed": {}, "global": [],
                "lines": ["first render: {0} clip(s)".format(n)], "first": True}
    a, b = _clip_map(old), _clip_map(new)
    added = [i for i in b if i not in a]
    removed = [i for i in a if i not in b]
    common_old = [i for i in a if i in b]
    common_new = [i for i in b if i in a]
    moved = _moved(common_old, common_new)
    changed: Dict[str, List[str]] = {}
    for i in common_new:
        lines = _clip_changes(a[i], b[i])
        if lines:
            changed[i] = lines
    glob = _global_changes(old, new)
    lines: List[str] = []
    for i in added:
        c = b[i]
        lines.append("added {0}{1}".format(i, " ({0})".format(c["label"]) if c.get("label") else ""))
    for i in removed:
        c = a[i]
        lines.append("removed {0}{1}".format(i, " ({0})".format(c["label"]) if c.get("label") else ""))
    if moved:
        lines.append("reordered: {0} clip(s) moved{1}".format(len(moved), " (" + ", ".join(moved[:5]) + (" ..." if len(moved) > 5 else "") + ")"))
    for i, ls in changed.items():
        for l in ls:
            lines.append("{0}: {1}".format(i, l))
    lines.extend(glob)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + ["... and {0} more".format(len(lines) - MAX_LINES)]
    if not lines:
        lines = ["no change to the spec (a re-render)"]
    return {"added": added, "removed": removed, "moved": moved, "changed": changed, "global": glob, "lines": lines, "first": False}


def summary(d: Dict[str, Any]) -> str:
    """One line for a listing: `+2 -1 ~5 clips`."""
    if d.get("first"):
        return d["lines"][0]
    bits = []
    if d["added"]:
        bits.append("+{0}".format(len(d["added"])))
    if d["removed"]:
        bits.append("-{0}".format(len(d["removed"])))
    if d["changed"]:
        bits.append("~{0}".format(len(d["changed"])))
    s = " ".join(bits) + (" clips" if bits else "")
    if d["global"]:
        s += (", " if s else "") + "{0} global".format(len(d["global"]))
    return s or "no spec change"
