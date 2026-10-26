"""Beats and transitions with intent: the named moves of an editor, as spec fragments (pure functions, no IO).

An edit that only cuts and zooms looks like a machine made it. The moves a person reaches for - hold the frame and
punch in on a punchline, replay it slower, hit an impact, drop a caption on the beat, whip into the next chapter - are
all expressible with the engine's primitives (freezes, speed keys, `fx`, overlays, SFX, transitions); this module names
them, so a lineup says `{"do": "freeze_zoom", "at": 4152.3}` instead of nine fields that have to agree.

A *beat* is `{"do": <macro>, "at": <source second>, ...params}` on a lineup clip; `expand(beat, ctx)` returns a
fragment `{freezes, fx, punches, overlays, markers, sfx, after, warnings}` that `edittemplates.episode` merges into the
clip (`punches` become camera keys there, so the webcam stays in frame).
`after` is clips to insert straight after this one (a replay). Positions are source seconds, durations are output
seconds, as everywhere in the engine.

A *transition preset* (`whip`, `glitch`, `zoom_through`, `flash`, `dip`, `crossfade`) is a chapter-level choice,
`apply_transition`. They were chosen by looking at each candidate between two real clips (docs/edit-enhancements-plan.md
0.10): xfade's `hblur` is a real whip-pan streak and `pixelize` a convincing glitch, but xfade's `zoomin` dips through a
muddy brown-black at its midpoint, so `zoom_through` is a camera push on both clips under a plain cross-fade instead.

Nothing here decides *when* a beat belongs: that is taste, and the author's. Restrained edits (Teo's series) use few.
"""

from typing import Any, Dict, List, Optional

from .specerror import SpecError

# how a beat names a sound: a role looked up in the lineup's `beat_sfx` ({role: {asset|file, gain_db}}); a role with no
# entry plays nothing (and says so in `warnings`) - an edit without the library's sounds still renders
SFX_ROLES = ("hit", "whoosh", "glitch", "scratch")

MARGIN = 0.3            # a beat this close to a clip's edge has no picture on one side of it
MACROS: Dict[str, Dict[str, Any]] = {
    "freeze_zoom": {
        "does": "hold the frame and punch in on it - the punchline, the reveal, the 'wait, what'",
        "params": "at (source s), hold=0.7 s, zoom=1.22, flash=0.4 (0 = none), sfx='hit', text=None, text_y=0.78",
    },
    "replay": {
        "does": "play the moment again after it, slower, tagged REPLAY (inserts a clip after this one)",
        "params": "at (source s), back=2.2 s before, forward=1.0 s after, rate=0.5, tag='REPLAY', sfx='whoosh'",
    },
    "impact": {
        "does": "a hit: screen shake, a flash and a sound, optionally a sticker (an explosion, a skull)",
        "params": "at (source s), shake=10 px, flash=0.45 (0 = none), sticker=None (asset id or role), sfx='hit'",
    },
    "caption_pop": {
        "does": "a big caption popping on the beat, optionally with a sticker and a sound",
        "params": "at (source s), text, dur=2.2 s, y=0.78, sticker=None, sfx='hit'",
    },
}

TRANSITIONS: Dict[str, Dict[str, Any]] = {
    "whip": {"does": "a whip-pan: a fast horizontal streak into the next clip (xfade hblur, 0.3 s) with a whoosh", "dur": 0.3},
    "glitch": {"does": "a digital glitch: the picture pixelates into the next clip (xfade pixelize, 0.2 s)", "dur": 0.2},
    "zoom_through": {"does": "push in on the outgoing clip and out of the incoming one under a cross-fade (0.3 s)", "dur": 0.3},
    "flash": {"does": "a white flash on the incoming clip (0.15 s)", "dur": 0.15},
    "dip": {"does": "dip through black (0.3 s)", "dur": 0.3},
    "crossfade": {"does": "a plain cross-fade (0.4 s)", "dur": 0.4},
}


def _num(beat: Dict[str, Any], key: str, default: Optional[float], lo: float, hi: float, name: str) -> float:
    v = beat.get(key, default)
    if v is None:
        raise SpecError("beat {0}: `{1}` is required".format(name, key))
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise SpecError("beat {0}: `{1}` must be a number (got {2!r})".format(name, key, v))
    if not lo <= v <= hi:
        raise SpecError("beat {0}: `{1}` must be between {2:g} and {3:g} (got {4:g})".format(name, key, lo, hi, v))
    return v


def _unknown(beat: Dict[str, Any], allowed: List[str], name: str) -> None:
    extra = sorted(set(beat) - set(allowed) - {"do"})
    if extra:
        raise SpecError("beat {0}: unknown parameter(s) {1}; it takes {2}".format(name, ", ".join(extra), ", ".join(allowed)))


class Ctx:
    """What a beat needs to know about the clip it is on."""

    def __init__(self, cid: str, src: str, lo: float, hi: float, sfx: Optional[Dict[str, Dict[str, Any]]] = None,
                 stickers: Optional[Dict[str, str]] = None, brand: Optional[Dict[str, Any]] = None,
                 grade: Optional[Dict[str, Any]] = None, gain_db: float = 0.0, n: int = 0, max_gap: float = 5.0) -> None:
        self.cid, self.src, self.lo, self.hi = cid, src, float(lo), float(hi)
        self.sfx = sfx or {}
        self.stickers = stickers or {}
        self.brand = brand or {}
        self.grade = grade
        self.gain_db = gain_db
        self.n = n                                     # beats already placed on this clip: keeps ids unique
        self.max_gap = max_gap                         # the profile's longest stretch with nothing new on screen


def _empty() -> Dict[str, Any]:
    # `punches` are camera zooms `{at, zoom}`, not fx: a camera key is what safe framing keeps the webcam inside of,
    # and an fx `punch` zooms around the centre of the frame and crops a corner webcam (HARD-RULES 38)
    return {"freezes": [], "fx": [], "punches": [], "overlays": [], "markers": [], "sfx": [], "after": [], "warnings": []}


def _sound(frag: Dict[str, Any], ctx: Ctx, role: Optional[str], clip: str, at: float, tag: str) -> None:
    if not role:
        return
    if role not in SFX_ROLES:
        raise SpecError("sfx must be one of {0} (got {1!r})".format(", ".join(SFX_ROLES), role))
    item = ctx.sfx.get(role)
    if not item:
        frag["warnings"].append("{0}: no `{1}` sound in the lineup's beat_sfx, so it plays none".format(tag, role))
        return
    frag["sfx"].append(dict(item, id=("b{0}{1}".format(ctx.cid, tag))[:24], on={"clip": clip, "at": round(at, 3)}))


def _within(ctx: Ctx, at: float, name: str) -> None:
    if not (ctx.lo + MARGIN <= at <= ctx.hi - MARGIN):
        raise SpecError("beat {0} at {1:g}: outside the clip's picture ({2:g}-{3:g}, {4:g} s kept clear at each edge)".format(
            name, at, ctx.lo, ctx.hi, MARGIN))


def _sticker(ctx: Ctx, ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    return ctx.stickers.get(ref, ref) if not str(ref).startswith("stk_") else ref


def _title_style(ctx: Ctx) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if ctx.brand.get("title_style"):
        out["style"] = ctx.brand["title_style"]
    return out


# --------------------------------------------------------------------------
# beats
# --------------------------------------------------------------------------


def freeze_zoom(beat: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    name = "freeze_zoom"
    _unknown(beat, ["at", "hold", "zoom", "flash", "sfx", "text", "text_y"], name)
    at = _num(beat, "at", None, 0.0, 1e6, name)
    hold = _num(beat, "hold", 0.7, 0.15, 3.0, name)
    zoom = _num(beat, "zoom", 1.22, 1.02, 1.6, name)
    flash = _num(beat, "flash", 0.4, 0.0, 1.0, name)
    _within(ctx, at, name)
    f = _empty()
    f["freezes"].append({"at": round(at, 3), "dur": round(hold, 3)})
    f["punches"].append({"at": round(at, 3), "zoom": round(zoom, 3)})
    if flash > 0:
        f["fx"].append({"type": "flash", "at": round(at, 3), "dur": 0.12, "amount": round(flash, 3)})
    f["markers"].append({"at": round(at, 3), "name": "beat"})
    _sound(f, ctx, beat.get("sfx", "hit"), ctx.cid, at, "f{0}".format(ctx.n))
    if beat.get("text"):
        f["overlays"].append(dict({"kind": "text", "at": round(at, 3), "dur": round(hold + 1.0, 3), "text": str(beat["text"]).upper(),
                                   "x": 0.5, "y": round(float(beat.get("text_y", 0.78)), 3), "size": 0.075, "plate": False,
                                   "anim_in": "pop", "anim_out": "fade"}, **_title_style(ctx)))
    return f


def impact(beat: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    name = "impact"
    _unknown(beat, ["at", "shake", "flash", "sticker", "sfx"], name)
    at = _num(beat, "at", None, 0.0, 1e6, name)
    shake = _num(beat, "shake", 10.0, 1.0, 40.0, name)
    flash = _num(beat, "flash", 0.45, 0.0, 1.0, name)
    _within(ctx, at, name)
    f = _empty()
    f["fx"].append({"type": "shake", "at": round(at, 3), "dur": 0.5, "amount": round(shake, 2), "freq": 14.0, "decay": True})
    if flash > 0:
        f["fx"].append({"type": "flash", "at": round(at, 3), "dur": 0.12, "amount": round(flash, 3)})
    asset = _sticker(ctx, beat.get("sticker"))
    if asset:
        f["fx"].append({"type": "sticker", "asset": asset, "at": round(at, 3), "dur": 1.2, "x": 0.5, "y": 0.4, "w": 0.2})
    f["markers"].append({"at": round(at, 3), "name": "beat"})
    _sound(f, ctx, beat.get("sfx", "hit"), ctx.cid, at, "i{0}".format(ctx.n))
    return f


def caption_pop(beat: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    name = "caption_pop"
    _unknown(beat, ["at", "text", "dur", "y", "sticker", "sfx"], name)
    at = _num(beat, "at", None, 0.0, 1e6, name)
    dur = _num(beat, "dur", 2.2, 0.6, 8.0, name)
    y = _num(beat, "y", 0.78, 0.05, 0.95, name)
    if not str(beat.get("text") or "").strip():
        raise SpecError("beat caption_pop: `text` is required")
    _within(ctx, at, name)
    f = _empty()
    f["overlays"].append(dict({"kind": "text", "at": round(at, 3), "dur": round(dur, 3), "text": str(beat["text"]).strip().upper()[:60],
                               "x": 0.5, "y": round(y, 3), "size": 0.08, "plate": False, "anim_in": "pop", "anim_out": "fade"},
                              **_title_style(ctx)))
    asset = _sticker(ctx, beat.get("sticker"))
    if asset:
        f["fx"].append({"type": "sticker", "asset": asset, "at": round(at, 3), "dur": round(dur, 3), "x": 0.5, "y": round(max(y - 0.16, 0.1), 3), "w": 0.14})
    f["markers"].append({"at": round(at, 3), "name": "beat"})
    _sound(f, ctx, beat.get("sfx", "hit"), ctx.cid, at, "c{0}".format(ctx.n))
    return f


def _push(lo: float, hi: float, rate: float, max_gap: float) -> List[Dict[str, Any]]:
    """A slow push-in across a slowed clip, with a key at least every 0.8 x the profile's gap: the pacing gate counts
    events, and seven seconds of slow motion with none is a dead stretch."""
    out_len = (hi - lo) / rate
    n = max(1, int(-(-out_len // (max_gap * 0.8))))
    return [{"at": round(lo + (hi - lo) * j / n, 3), "zoom": round(1.0 + 0.07 * j / n, 4), "ease": "inout2"} for j in range(n + 1)]


def replay(beat: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    name = "replay"
    _unknown(beat, ["at", "back", "forward", "rate", "tag", "sfx"], name)
    at = _num(beat, "at", None, 0.0, 1e6, name)
    back = _num(beat, "back", 2.2, 0.5, 10.0, name)
    fwd = _num(beat, "forward", 1.0, 0.3, 6.0, name)
    rate = _num(beat, "rate", 0.5, 0.25, 1.0, name)
    _within(ctx, at, name)
    lo, hi = max(0.0, at - back), at + fwd
    rid = "{0}r{1}".format(ctx.cid, ctx.n + 1)
    tag = str(beat.get("tag") or "REPLAY").upper()[:24]
    out_len = (hi - lo) / rate
    clip: Dict[str, Any] = {
        "id": rid, "src": ctx.src, "in": round(lo, 3), "out": round(hi, 3), "label": "replay of {0}".format(ctx.cid)[:80],
        "speed": rate, "interp": "blend", "tags": [],
        "overlays": [dict({"kind": "text", "at": round(lo + 0.1, 3), "dur": 2.4, "text": tag,
                           "x": 0.12, "y": 0.1, "size": 0.05, "plate": True, "anim_in": "slide_left", "anim_out": "fade"}, **_title_style(ctx))],
        "camera": _push(lo, hi, rate, ctx.max_gap),
        "markers": [{"at": round(at, 3), "name": "beat"}],
        "audio": {"slowmo": "pitch", "gain_db": ctx.gain_db},
        "transition_in": {"type": "flash", "dur": 0.15},
    }
    if ctx.grade:
        clip["grade"] = dict(ctx.grade)
    f = _empty()
    f["after"].append(clip)
    _sound(f, ctx, beat.get("sfx", "whoosh"), rid, lo, "r{0}".format(ctx.n))
    return f


_BEATS = {"freeze_zoom": freeze_zoom, "impact": impact, "caption_pop": caption_pop, "replay": replay}


def expand(beat: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    """The fragment for one beat. `SpecError` names what is wrong (an unknown move, a parameter out of range, a beat
    on the edge of a clip) - a beat is never silently dropped."""
    if not isinstance(beat, dict):
        raise SpecError("a beat is an object like {\"do\": \"freeze_zoom\", \"at\": 4152.3}")
    do = beat.get("do")
    if do not in _BEATS:
        raise SpecError("beat `do` must be one of {0} (got {1!r})".format(", ".join(sorted(_BEATS)), do))
    return _BEATS[do](beat, ctx)


def merge(parts: Dict[str, Any], frag: Dict[str, Any]) -> None:
    """Add a fragment's lists into `parts` (the clip being built); `after` and `warnings` accumulate too."""
    for key in ("freezes", "fx", "punches", "overlays", "markers", "sfx", "after", "warnings"):
        parts.setdefault(key, []).extend(frag.get(key, []))


# --------------------------------------------------------------------------
# transitions
# --------------------------------------------------------------------------


def _camera_at_end(camera: List[Dict[str, Any]], out: float, depth: float, span: float) -> Optional[List[Dict[str, Any]]]:
    """The camera keys of a clip with a push in over its last `span` seconds, or None when its own keys are in the way."""
    start = out - span
    keep = [k for k in camera if k["at"] < start - 0.05]
    if (keep and keep[-1]["zoom"] > 1.12) or any(k["zoom"] > 1.12 for k in camera if k["at"] >= start - 0.05):
        return None                                   # a punch is still up, or lands here: do not fight it (or drop its ease-back)
    keys = keep + [{"at": round(start, 3), "zoom": keep[-1]["zoom"] if keep else 1.0, "ease": "in2"},
                   {"at": round(out, 3), "zoom": depth, "ease": "in2"}]
    return keys


def _camera_at_start(camera: List[Dict[str, Any]], lo: float, depth: float, span: float) -> Optional[List[Dict[str, Any]]]:
    """The camera keys of a clip that starts pushed in and eases out over its first `span` seconds, or None when its own keys are in the way."""
    end = lo + span
    if any(k["zoom"] > 1.12 for k in camera if k["at"] <= end + 0.05):
        return None
    rest = [k for k in camera if k["at"] > end + 0.05]
    return [{"at": round(lo, 3), "zoom": depth, "ease": "out2"}, {"at": round(end, 3), "zoom": 1.0, "ease": "out2"}] + rest


def apply_transition(name: str, prev: Optional[Dict[str, Any]], nxt: Dict[str, Any], sfx: Optional[Dict[str, Dict[str, Any]]] = None,
                     depth: float = 1.35) -> Dict[str, Any]:
    """Set `nxt`'s `transition_in` to a preset and, for `zoom_through`, push the camera on both clips (in place).
    Returns `{"sfx": [...], "warnings": [...]}`. A preset that cannot be done here (a card on one side, a kill's punch in
    the way of the push) falls back to a plain cross-fade and says so."""
    if name not in TRANSITIONS:
        raise SpecError("transition must be one of {0} (got {1!r})".format(", ".join(sorted(TRANSITIONS)), name))
    out: Dict[str, Any] = {"sfx": [], "warnings": []}
    dur = TRANSITIONS[name]["dur"]
    if prev is None:
        out["warnings"].append("transition {0} on the first clip: nothing to transition from".format(name))
        return out
    if name == "whip":
        nxt["transition_in"] = {"type": "xfade", "name": "hblur", "dur": dur}
        role = "whoosh"
    elif name == "glitch":
        nxt["transition_in"] = {"type": "xfade", "name": "pixelize", "dur": dur}
        role = "glitch"
    elif name == "flash":
        nxt["transition_in"] = {"type": "flash", "dur": dur}
        role = "hit"
    elif name == "dip":
        nxt["transition_in"] = {"type": "dip", "dur": dur}
        role = None
    elif name == "crossfade":
        nxt["transition_in"] = {"type": "xfade", "name": "fade", "dur": dur}
        role = None
    else:                                                                # zoom_through
        nxt["transition_in"] = {"type": "xfade", "name": "fade", "dur": dur}
        role = "whoosh"
        if prev.get("type", "clip") == "clip" and nxt.get("type", "clip") == "clip" and prev.get("src") and nxt.get("src"):
            a = _camera_at_end(prev.get("camera") or [], float(prev["out"]), depth, dur + 0.1)
            b = _camera_at_start(nxt.get("camera") or [], float(nxt["in"]), depth, dur + 0.1)
            if a is not None and b is not None:
                prev["camera"], nxt["camera"] = a, b
            else:
                out["warnings"].append("zoom_through into {0}: a punch is in the way of the push, so it is a plain cross-fade".format(nxt.get("id")))
        else:
            out["warnings"].append("zoom_through into {0}: a card has no picture to push, so it is a plain cross-fade".format(nxt.get("id")))
    if role and sfx and sfx.get(role) and nxt.get("src") is not None:
        out["sfx"].append(dict(sfx[role], id=("t" + str(nxt["id"]))[:24], on={"clip": nxt["id"], "at": round(float(nxt["in"]), 3)}))
    elif role and not (sfx and sfx.get(role)):
        out["warnings"].append("transition {0}: no `{1}` sound in the lineup's beat_sfx, so it is silent".format(name, role))
    return out
