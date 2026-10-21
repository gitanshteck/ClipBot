"""Edit specs: the declarative description of a complete video edit.

Pure validation and defaults - no IO, no ffmpeg, no settings, exactly like
`reelspec.py` and `fxspec.py`, and for the same reason: the CLI validates a spec
and the renderer resolves it through ONE implementation, so what `clipbot edit
validate` accepts is what `clipbot edit render` will build.

An *edit* is a list of clips (cut from any mix of YouTube videos, Kick VODs and
plain files), each with its own retiming, camera moves, grade, overlays and
audio, plus timeline tracks (music, sound effects) that hang off clips instead of
off absolute times, and a mastering target. `reelspec`/`fxspec` stay what they
are - the vertical reel path - and `fxspec`'s effect list is reused verbatim as a
clip's `fx`, so an effect authored in the dashboard means the same thing here.

Time base
---------
Every *position* in a clip (`in`, `out`, a speed key's `at`, a camera key, a text
overlay's `at`, a marker, an fx `at`) is a **source second**: the same clock as
the stream's transcript, the kill feed and the dashboard. Every *duration*
(`dur`, `in_dur`, a transition) is an **output second**: what the viewer sees.

That split is the one invariant that matters. It makes a callout land on the kill
whether or not a slow-motion ramp stretched the moment, and it means trimming a
clip's `in` never walks an effect off the footage it was authored against. It
mirrors `fxspec`'s "absolute VOD time" rule on purpose, and it is also why times
that hang off *another clip* are written as an anchor (`{"clip", "marker",
"offset"}`) and resolved by `edittimeline` after the timeline exists: a time
written as "42 s into the video" breaks the moment a clip before it changes
length.

Ideas borrowed (MIT, github.com/ranahaani/i-hate-editing): audio-peak alignment of
sound effects, a hard pacing gate (no stretch with nothing new on screen), a 30 ms
fade on every cut, mastering to a loudness target, and "every rule records a
failure" - see `.claude/skills/clipbot-edit/`.
"""

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import fxspec
from .specerror import SpecError

VERSION = 1

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

MAX_CLIPS = 400
MAX_SPEED_KEYS = 12
MAX_CAMERA_KEYS = 24
MAX_OVERLAYS = 24
MAX_MARKERS = 24

# --------------------------------------------------------------------------
# easing
# --------------------------------------------------------------------------

# Never `linear` for a move the viewer is meant to feel (rules/motion.md): `out`
# is fast-in/slow-landing for arrivals, `inout` for continuous drifts.
EASES = ("linear", "in2", "out2", "inout2", "in3", "out3", "inout3")


def ease_value(kind: str, p: float) -> float:
    """The easing curve at progress p in [0, 1]. `editfilters.ease_expr` builds
    the ffmpeg-expression twin of this; a test keeps the two in step."""
    p = 0.0 if p < 0.0 else 1.0 if p > 1.0 else p
    if kind == "linear":
        return p
    if kind == "in2":
        return p * p
    if kind == "out2":
        return 1.0 - (1.0 - p) ** 2
    if kind == "inout2":
        return 2.0 * p * p if p < 0.5 else 1.0 - (-2.0 * p + 2.0) ** 2 / 2.0
    if kind == "in3":
        return p ** 3
    if kind == "out3":
        return 1.0 - (1.0 - p) ** 3
    if kind == "inout3":
        return 4.0 * p ** 3 if p < 0.5 else 1.0 - (-2.0 * p + 2.0) ** 3 / 2.0
    raise SpecError("unknown ease {0!r} (use one of {1})".format(kind, ", ".join(EASES)))


# --------------------------------------------------------------------------
# presets and profiles
# --------------------------------------------------------------------------

# A profile is a *taste* knob: the same words an editor's client uses ("punchier",
# "slower"). It sets defaults and the pacing gate, never a hard limit on what a
# spec may say.
PROFILES = {
    "punchy": {
        "max_gap": 4.0,        # seconds with nothing new on screen before the gate fails
        "transition": 0.0,     # hard cuts
        "punch": 1.12,
        "slowmo_rate": 0.35,
        "pre_roll": 6.0,
        "post_roll": 3.0,
        "sfx_per_15s": 5,
        "flash": True,
    },
    "balanced": {
        "max_gap": 5.0,
        "transition": 0.2,
        "punch": 1.10,
        "slowmo_rate": 0.5,
        "pre_roll": 7.0,
        "post_roll": 4.0,
        "sfx_per_15s": 4,
        "flash": False,
    },
    "restrained": {
        "max_gap": 6.5,
        "transition": 0.4,
        "punch": 1.06,
        "slowmo_rate": 0.6,
        "pre_roll": 8.0,
        "post_roll": 5.0,
        "sfx_per_15s": 3,
        "flash": False,
    },
}

# Output presets follow the platform's own upload guidance (YouTube: H.264 High,
# yuv420p, 2 B-frames, closed GOP of half the frame rate, AAC 48 kHz, faststart).
OUTPUT_PRESETS = {
    "youtube_1080p60": {
        "width": 1920, "height": 1080, "fps": 60, "fit": "fit",
        "crf": 18, "x264_preset": "slow", "audio_bitrate": "192k", "sample_rate": 48000,
        "gop_seconds": 0.5, "bframes": 2, "faststart": True, "preview": False,
    },
    "youtube_1080p30": {
        "width": 1920, "height": 1080, "fps": 30, "fit": "fit",
        "crf": 18, "x264_preset": "slow", "audio_bitrate": "192k", "sample_rate": 48000,
        "gop_seconds": 0.5, "bframes": 2, "faststart": True, "preview": False,
    },
    "shorts_1080x1920": {
        "width": 1080, "height": 1920, "fps": 60, "fit": "blur",
        "crf": 19, "x264_preset": "slow", "audio_bitrate": "192k", "sample_rate": 48000,
        "gop_seconds": 0.5, "bframes": 2, "faststart": True, "preview": False,
    },
    "square_1080": {
        "width": 1080, "height": 1080, "fps": 60, "fit": "blur",
        "crf": 19, "x264_preset": "slow", "audio_bitrate": "192k", "sample_rate": 48000,
        "gop_seconds": 0.5, "bframes": 2, "faststart": True, "preview": False,
    },
    "portrait_4x5": {
        "width": 1080, "height": 1350, "fps": 60, "fit": "blur",
        "crf": 19, "x264_preset": "slow", "audio_bitrate": "192k", "sample_rate": 48000,
        "gop_seconds": 0.5, "bframes": 2, "faststart": True, "preview": False,
    },
    # Fast enough to iterate on. The renderer swaps in a hardware encoder when one
    # is present; the picture is a proxy and is never delivered.
    "preview_540p": {
        "width": 960, "height": 540, "fps": 30, "fit": "fit",
        "crf": 27, "x264_preset": "ultrafast", "audio_bitrate": "96k", "sample_rate": 48000,
        "gop_seconds": 1.0, "bframes": 0, "faststart": True, "preview": True,
    },
}

FITS = ("fit", "fill", "blur")

# libavfilter's xfade names (ffmpeg 8). `fade` is the plain crossfade.
XFADE_NAMES = (
    "fade", "fadeblack", "fadewhite", "fadegrays", "distance", "dissolve",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circlecrop", "circleopen", "circleclose", "rectcrop",
    "vertopen", "vertclose", "horzopen", "horzclose",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "radial", "zoomin", "pixelize", "squeezeh", "squeezev",
    "hlwind", "hrwind", "vuwind", "vdwind", "fadebright",
)
TRANSITION_TYPES = ("cut", "dip", "xfade", "flash")

GRADE_PRESETS = ("none", "neutral_punch", "warm_lift", "cool_clean", "gamer_pop")
GRADE_STRENGTHS = ("subtle", "normal", "strong")

# Every rate the renderer can express. atempo takes 0.5..2.0 per instance; the
# audio builder chains it, so a 0.25x ramp is two instances.
SPEED_RANGE = (0.25, 4.0)
ZOOM_RANGE = (1.0, 3.0)


# --------------------------------------------------------------------------
# tiny validators (every message starts with the spec path, so a 400-line spec
# points at the field instead of at "invalid")
# --------------------------------------------------------------------------


def _fail(path: str, message: str) -> None:
    raise SpecError("{0}: {1}".format(path, message))


def _dict(value: Any, path: str, allowed: Tuple[str, ...]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail(path, "unknown key(s) {0} (allowed: {1})".format(
            ", ".join(repr(u) for u in unknown), ", ".join(allowed)))
    return value


def _num(value: Any, path: str, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a number, got {0!r}".format(value))
    v = float(value)
    if math.isnan(v) or math.isinf(v):
        _fail(path, "must be a finite number")
    if lo is not None and v < lo:
        _fail(path, "must be >= {0}, got {1}".format(lo, v))
    if hi is not None and v > hi:
        _fail(path, "must be <= {0}, got {1}".format(hi, v))
    return v


def _int(value: Any, path: str, lo: int, hi: int) -> int:
    v = _num(value, path, lo, hi)
    if v != int(v):
        _fail(path, "must be a whole number, got {0}".format(v))
    return int(v)


def _choice(value: Any, path: str, options) -> str:
    if value not in options:
        _fail(path, "{0!r} is not one of {1}".format(value, ", ".join(str(o) for o in options)))
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be true or false")
    return value


def _str(value: Any, path: str, max_len: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    if len(value) > max_len:
        _fail(path, "is {0} characters, the limit is {1}".format(len(value), max_len))
    return value


def _color(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _COLOR_RE.match(value):
        _fail(path, "must be a #RRGGBB colour, got {0!r}".format(value))
    return value.upper()


def _list(value: Any, path: str, max_len: int) -> List[Any]:
    if not isinstance(value, list):
        _fail(path, "must be a list")
    if len(value) > max_len:
        _fail(path, "has {0} entries, the limit is {1}".format(len(value), max_len))
    return value


# --------------------------------------------------------------------------
# sub-structures
# --------------------------------------------------------------------------


def _anchor(raw: Any, path: str, clip_ids: List[str]) -> Dict[str, Any]:
    """Where on the timeline a track item sits: a point relative to a clip.

    {"clip": "n1"}                         the clip's first frame
    {"clip": "n1", "marker": "kill"}       a named marker inside the clip
    {"clip": "n1", "at": 9930.1}           a source second of that clip
    {"clip": "n1", "from": "end"}          the clip's last frame
    plus "offset" (output seconds, may be negative).
    """
    d = _dict(raw, path, ("clip", "marker", "at", "from", "offset"))
    clip = _str(d.get("clip"), path + ".clip", 32)
    if clip not in clip_ids:
        _fail(path + ".clip", "no clip with id {0!r}".format(clip))
    out = {"clip": clip, "offset": _num(d.get("offset", 0.0), path + ".offset", -600.0, 600.0)}
    picked = [k for k in ("marker", "at", "from") if k in d]
    if len(picked) > 1:
        _fail(path, "give at most one of marker / at / from")
    if "marker" in d:
        out["marker"] = _str(d["marker"], path + ".marker", 32)
    if "at" in d:
        out["at"] = _num(d["at"], path + ".at", 0.0, 1e6)
    if "from" in d:
        out["from"] = _choice(d["from"], path + ".from", ("start", "end"))
    return out


def _speed(raw: Any, path: str) -> List[Dict[str, Any]]:
    """`speed` is a number (constant rate over the whole clip) or a list of keys.

    A key is {"at": source second, "rate": 0.25..4.0, "ease": ...}: the rate is
    that value at `at` and eases to the next key's. Outside the first and last key
    the rate holds. This is the speed ramp every gaming montage is built on - see
    rules/motion.md for the shapes that work (fast approach, slow kill, snap back).
    """
    if raw is None:
        return []
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return [{"at": None, "rate": _num(raw, path, *SPEED_RANGE), "ease": "linear"}]
    keys = _list(raw, path, MAX_SPEED_KEYS)
    out = []
    for i, k in enumerate(keys):
        p = "{0}[{1}]".format(path, i)
        d = _dict(k, p, ("at", "rate", "ease"))
        out.append({
            "at": _num(d.get("at"), p + ".at", 0.0, 1e6),
            "rate": _num(d.get("rate"), p + ".rate", *SPEED_RANGE),
            "ease": _choice(d.get("ease", "inout2"), p + ".ease", EASES),
        })
    out.sort(key=lambda k: k["at"])
    for a, b in zip(out, out[1:]):
        if b["at"] - a["at"] < 0.05:
            _fail(path, "keys at {0} and {1} are closer than 0.05 s".format(a["at"], b["at"]))
    return out


def _freezes(raw: Any, path: str) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    out = []
    for i, f in enumerate(_list(raw, path, 6)):
        p = "{0}[{1}]".format(path, i)
        d = _dict(f, p, ("at", "dur"))
        out.append({"at": _num(d.get("at"), p + ".at", 0.0, 1e6),
                    "dur": _num(d.get("dur", 0.5), p + ".dur", 0.05, 5.0)})
    out.sort(key=lambda f: f["at"])
    return out


def _camera(raw: Any, path: str) -> List[Dict[str, Any]]:
    """Camera keys: zoom (and pan) at source seconds, eased between keys.

    {"at": s, "zoom": 1.0..3.0, "x": 0..1, "y": 0..1, "ease": ...}. x/y are the
    centre of the visible window as fractions of the frame (default centre).
    The segment from key A to key B uses B's ease. Holding a zoom is two keys with
    the same value; rules/motion.md says to hold, not ride.
    """
    if raw is None:
        return []
    out = []
    for i, k in enumerate(_list(raw, path, MAX_CAMERA_KEYS)):
        p = "{0}[{1}]".format(path, i)
        d = _dict(k, p, ("at", "zoom", "x", "y", "ease"))
        out.append({
            "at": _num(d.get("at"), p + ".at", 0.0, 1e6),
            "zoom": _num(d.get("zoom", 1.0), p + ".zoom", *ZOOM_RANGE),
            "x": _num(d.get("x", 0.5), p + ".x", 0.0, 1.0),
            "y": _num(d.get("y", 0.5), p + ".y", 0.0, 1.0),
            "ease": _choice(d.get("ease", "out2"), p + ".ease", EASES),
        })
    out.sort(key=lambda k: k["at"])
    return out


def _grade(raw: Any, path: str) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    d = _dict(raw, path, ("preset", "strength", "window", "ease", "custom"))
    out = {
        "preset": _choice(d.get("preset", "none"), path + ".preset", GRADE_PRESETS),
        "strength": _choice(d.get("strength", "normal"), path + ".strength", GRADE_STRENGTHS),
    }
    if "window" in d:
        w = d["window"]
        if not (isinstance(w, list) and len(w) == 2):
            _fail(path + ".window", "must be [start, end] in source seconds")
        a, b = _num(w[0], path + ".window[0]", 0.0, 1e6), _num(w[1], path + ".window[1]", 0.0, 1e6)
        if b <= a:
            _fail(path + ".window", "end must be after start")
        out["window"] = [a, b]
        out["ease"] = _num(d.get("ease", 0.1), path + ".ease", 0.0, 2.0)
    if "custom" in d:
        c = _dict(d["custom"], path + ".custom",
                  ("brightness", "contrast", "saturation", "gamma", "temperature", "sharpen"))
        out["custom"] = {
            "brightness": _num(c.get("brightness", 0.0), path + ".custom.brightness", -0.5, 0.5),
            "contrast": _num(c.get("contrast", 1.0), path + ".custom.contrast", 0.5, 2.0),
            "saturation": _num(c.get("saturation", 1.0), path + ".custom.saturation", 0.0, 3.0),
            "gamma": _num(c.get("gamma", 1.0), path + ".custom.gamma", 0.5, 2.0),
            "temperature": _num(c.get("temperature", 6500.0), path + ".custom.temperature", 2500.0, 12000.0),
            "sharpen": _num(c.get("sharpen", 0.0), path + ".custom.sharpen", 0.0, 1.5),
        }
    return out


def _framing(raw: Any, path: str) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    d = _dict(raw, path, ("mode", "focus", "crop"))
    out: Dict[str, Any] = {}
    if "mode" in d:
        out["mode"] = _choice(d["mode"], path + ".mode", FITS)
    if "focus" in d:
        f = _dict(d["focus"], path + ".focus", ("x", "y"))
        out["focus"] = {"x": _num(f.get("x", 0.5), path + ".focus.x", 0.0, 1.0),
                        "y": _num(f.get("y", 0.5), path + ".focus.y", 0.0, 1.0)}
    if "crop" in d:
        c = _dict(d["crop"], path + ".crop", ("x", "y", "w", "h"))
        x = _num(c.get("x", 0.0), path + ".crop.x", 0.0, 0.95)
        y = _num(c.get("y", 0.0), path + ".crop.y", 0.0, 0.95)
        w = _num(c.get("w", 1.0), path + ".crop.w", 0.05, 1.0)
        h = _num(c.get("h", 1.0), path + ".crop.h", 0.05, 1.0)
        if x + w > 1.0 + 1e-6 or y + h > 1.0 + 1e-6:
            _fail(path + ".crop", "the rectangle runs off the frame")
        out["crop"] = {"x": x, "y": y, "w": w, "h": h}
    return out


def _clip_audio(raw: Any, path: str) -> Dict[str, Any]:
    d = _dict(raw if raw is not None else {}, path, ("gain_db", "mute", "fade_ms", "slowmo"))
    return {
        "gain_db": _num(d.get("gain_db", 0.0), path + ".gain_db", -40.0, 12.0),
        "mute": _bool(d.get("mute", False), path + ".mute"),
        # 30 ms removes the pop of a hard audio cut (rules/audio.md); 0 turns it off.
        "fade_ms": _num(d.get("fade_ms", 30.0), path + ".fade_ms", 0.0, 500.0),
        # What the game audio does while the picture is slowed: keep it
        # pitch-correct, drop its pitch with the speed, or mute it.
        "slowmo": _choice(d.get("slowmo", "tempo"), path + ".slowmo", ("tempo", "pitch", "mute")),
    }


def _transition(raw: Any, path: str, default_dur: float) -> Dict[str, Any]:
    if raw is None:
        return {"type": "cut", "dur": 0.0}
    if isinstance(raw, str):
        raw = {"type": raw}
    d = _dict(raw, path, ("type", "name", "dur", "color"))
    kind = _choice(d.get("type", "cut"), path + ".type", TRANSITION_TYPES)
    out: Dict[str, Any] = {"type": kind}
    if kind == "cut":
        out["dur"] = 0.0
        return out
    out["dur"] = _num(d.get("dur", default_dur or 0.25), path + ".dur", 0.05, 3.0)
    if kind == "xfade":
        out["name"] = _choice(d.get("name", "fade"), path + ".name", XFADE_NAMES)
    if kind in ("dip", "flash"):
        out["color"] = _color(d.get("color", "#000000" if kind == "dip" else "#FFFFFF"), path + ".color")
    return out


OVERLAY_KINDS = ("text", "callout", "image")
ANIM_IN = ("none", "fade", "pop", "slide_left", "slide_right", "slide_up", "slide_down")
ANIM_OUT = ("none", "fade", "slide")


def _overlay(raw: Any, path: str) -> Dict[str, Any]:
    """A text, callout or image item laid over a clip.

    Rendered to a transparent PNG first (Pillow: library fonts, outline, plate)
    and composited with an animated position/alpha, because drawtext cannot do a
    slide-and-settle and depends on fontconfig, which the Windows ffmpeg builds
    ship without. `at` is the source second it appears; `dur` is how long it stays.
    """
    d = _dict(raw, path, (
        "id", "kind", "at", "dur", "text", "image", "style", "x", "y", "w", "anchor",
        "anim_in", "anim_out", "in_dur", "out_dur", "size", "color", "plate", "accent", "sub",
    ))
    kind = _choice(d.get("kind", "text"), path + ".kind", OVERLAY_KINDS)
    out: Dict[str, Any] = {
        "kind": kind,
        "at": _num(d.get("at"), path + ".at", 0.0, 1e6),
        "dur": _num(d.get("dur", 2.0), path + ".dur", 0.2, 30.0),
        "x": _num(d.get("x", 0.5), path + ".x", -0.5, 1.5),
        "y": _num(d.get("y", 0.15), path + ".y", -0.5, 1.5),
        "w": _num(d.get("w", 0.30), path + ".w", 0.02, 1.0),
        "anchor": _choice(d.get("anchor", "center"), path + ".anchor", ("center", "left", "right")),
        "anim_in": _choice(d.get("anim_in", "pop" if kind == "callout" else "fade"), path + ".anim_in", ANIM_IN),
        "anim_out": _choice(d.get("anim_out", "fade"), path + ".anim_out", ANIM_OUT),
        "in_dur": _num(d.get("in_dur", 0.22), path + ".in_dur", 0.0, 2.0),
        "out_dur": _num(d.get("out_dur", 0.2), path + ".out_dur", 0.0, 2.0),
    }
    if "id" in d:
        if not isinstance(d["id"], str) or not _ID_RE.match(d["id"]):
            _fail(path + ".id", "use 1-32 letters, digits, _ or -")
        out["id"] = d["id"]
    if out["dur"] < out["in_dur"] + out["out_dur"]:
        _fail(path, "dur {0} is shorter than in_dur + out_dur ({1})".format(
            out["dur"], round(out["in_dur"] + out["out_dur"], 3)))
    if kind in ("text", "callout"):
        out["text"] = _str(d.get("text"), path + ".text", 140)
        if "sub" in d:
            out["sub"] = _str(d["sub"], path + ".sub", 140)
        out["size"] = _num(d.get("size", 0.06 if kind == "text" else 0.05), path + ".size", 0.01, 0.30)
        out["color"] = _color(d.get("color", "#FFFFFF"), path + ".color")
        if "accent" in d:
            out["accent"] = _color(d["accent"], path + ".accent")
        if "style" in d:
            out["style"] = d["style"] if isinstance(d["style"], str) else None
            if not isinstance(d["style"], str):
                _fail(path + ".style", "must be a library text-style id")
        out["plate"] = _bool(d.get("plate", kind == "callout"), path + ".plate")
    if kind in ("image", "callout") and "image" in d:
        out["image"] = _str(d["image"], path + ".image", 400)
    if kind == "image" and "image" not in out:
        _fail(path, "an image overlay needs an `image` path")
    return out


def _marker(raw: Any, path: str) -> Dict[str, Any]:
    d = _dict(raw, path, ("at", "name"))
    return {"at": _num(d.get("at"), path + ".at", 0.0, 1e6), "name": _str(d.get("name", "mark"), path + ".name", 32)}


# --------------------------------------------------------------------------
# clips
# --------------------------------------------------------------------------

CLIP_KEYS = (
    "id", "type", "src", "in", "out", "label", "speed", "interp", "freezes", "camera", "grade",
    "fx", "overlays", "markers", "audio", "framing", "transition_in", "tags",
)

# How slowed footage fills the frames between source frames. `dup` repeats frames
# (free, slightly steppy), `blend` cross-fades neighbours (cheap, soft), `mci`
# motion-interpolates (smoothest, slow, can smear at occlusions).
INTERPS = ("dup", "blend", "mci")
CARD_KEYS = ("id", "type", "dur", "bg", "label", "overlays", "audio", "transition_in", "tags", "markers")

FX_ALLOWED = ("punch", "shake", "flash", "sfx", "sticker", "text")


def _clip(raw: Any, path: str, sources: Dict[str, Any], default_trans: float) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _fail(path, "must be an object")
    kind = raw.get("type", "clip")
    if kind not in ("clip", "card"):
        _fail(path + ".type", "must be \"clip\" or \"card\"")
    d = _dict(raw, path, CARD_KEYS if kind == "card" else CLIP_KEYS)
    cid = d.get("id")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        _fail(path + ".id", "every clip needs an id of 1-32 letters, digits, _ or -")

    out: Dict[str, Any] = {"id": cid, "type": kind, "label": str(d.get("label", cid))[:80],
                           "tags": [str(t)[:24] for t in _list(d.get("tags", []), path + ".tags", 12)]}
    out["audio"] = _clip_audio(d.get("audio"), path + ".audio")
    out["transition_in"] = _transition(d.get("transition_in"), path + ".transition_in", default_trans)
    out["overlays"] = [_overlay(o, "{0}.overlays[{1}]".format(path, i))
                       for i, o in enumerate(_list(d.get("overlays", []), path + ".overlays", MAX_OVERLAYS))]
    out["markers"] = [_marker(m, "{0}.markers[{1}]".format(path, i))
                      for i, m in enumerate(_list(d.get("markers", []), path + ".markers", MAX_MARKERS))]

    if kind == "card":
        out["dur"] = _num(d.get("dur", 2.5), path + ".dur", 0.3, 30.0)
        out["bg"] = _color(d.get("bg", "#0B0B0F"), path + ".bg")
        return out

    src = d.get("src")
    if src not in sources:
        _fail(path + ".src", "{0!r} is not one of the sources ({1})".format(src, ", ".join(sorted(sources)) or "none"))
    out["src"] = src
    a = _num(d.get("in"), path + ".in", 0.0, 1e6)
    b = _num(d.get("out"), path + ".out", 0.0, 1e6)
    if b - a < 0.3:
        _fail(path, "out must be at least 0.3 s after in (got in={0}, out={1})".format(a, b))
    if b - a > 3600:
        _fail(path, "a single clip longer than an hour is almost certainly a typo")
    out["in"], out["out"] = a, b
    out["speed"] = _speed(d.get("speed"), path + ".speed")
    out["interp"] = _choice(d.get("interp", "dup"), path + ".interp", INTERPS)
    out["freezes"] = _freezes(d.get("freezes"), path + ".freezes")
    out["camera"] = _camera(d.get("camera"), path + ".camera")
    out["grade"] = _grade(d.get("grade"), path + ".grade")
    out["framing"] = _framing(d.get("framing"), path + ".framing")

    fx = d.get("fx", [])
    if fx:
        for i, e in enumerate(_list(fx, path + ".fx", fxspec.MAX_EFFECTS)):
            kind_ = e.get("type") if isinstance(e, dict) else None
            if kind_ not in FX_ALLOWED:
                _fail("{0}.fx[{1}].type".format(path, i),
                      "{0!r} is not usable in an edit (allowed: {1}); retiming lives in `speed`/`freezes`, "
                      "music in `tracks.music`".format(kind_, ", ".join(FX_ALLOWED)))
        # An edit is written by hand or by a template, so an effect may omit the id
        # the dashboard would have generated.
        fx = [dict(e, id=e.get("id", "fx{0}".format(i + 1))) for i, e in enumerate(fx)]
        try:
            out["fx"] = fxspec.normalize_fx(fx)
        except SpecError as exc:
            _fail(path + ".fx", str(exc))
    else:
        out["fx"] = []

    # everything positional must sit inside the clip's source window
    for where, items in (("speed", out["speed"]), ("camera", out["camera"]), ("markers", out["markers"]),
                         ("overlays", out["overlays"]), ("freezes", out["freezes"])):
        for i, it in enumerate(items):
            at = it.get("at")
            if at is not None and not (a - 1e-6 <= at <= b + 1e-6):
                _fail("{0}.{1}[{2}].at".format(path, where, i),
                      "{0} is outside the clip's source window {1}..{2}".format(at, a, b))
    return out


# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------


def _asset_or_file(d: Dict[str, Any], path: str) -> Dict[str, Any]:
    has_asset, has_file = "asset" in d, "file" in d
    if has_asset == has_file:
        _fail(path, "give exactly one of `asset` (a library id) or `file` (an absolute path)")
    if has_asset:
        aid = d["asset"]
        if not isinstance(aid, str) or not re.match(r"^[a-z]{3}_[0-9a-f]{8,32}$", aid):
            _fail(path + ".asset", "must be a library asset id like mus_ab12cd34")
        return {"asset": aid}
    f = _str(d["file"], path + ".file", 500)
    if not Path(f).is_absolute():
        _fail(path + ".file", "must be an absolute path")
    return {"file": f}


def _duck(raw: Any, path: str) -> Dict[str, Any]:
    base = dict(fxspec.DEFAULT_DUCK)
    if raw is False:
        base["enabled"] = False
        return base
    if raw in (None, True):
        return base
    d = _dict(raw, path, ("threshold", "ratio", "attack", "release", "enabled"))
    base["enabled"] = _bool(d.get("enabled", True), path + ".enabled")
    base["threshold"] = _num(d.get("threshold", base["threshold"]), path + ".threshold", 0.001, 1.0)
    base["ratio"] = _num(d.get("ratio", base["ratio"]), path + ".ratio", 1.0, 20.0)
    base["attack"] = _num(d.get("attack", base["attack"]), path + ".attack", 0.01, 2000.0)
    base["release"] = _num(d.get("release", base["release"]), path + ".release", 0.01, 9000.0)
    return base


def _music(raw: Any, path: str, clip_ids: List[str]) -> Dict[str, Any]:
    d = _dict(raw, path, ("id", "asset", "file", "start", "end", "gain_db", "duck", "fade_in", "fade_out",
                          "offset", "loop"))
    out = _asset_or_file(d, path)
    out["id"] = str(d.get("id", "music"))[:32]
    out["start"] = "start" if d.get("start", "start") == "start" else _anchor(d["start"], path + ".start", clip_ids)
    out["end"] = "end" if d.get("end", "end") == "end" else _anchor(d["end"], path + ".end", clip_ids)
    # Beds sit at -18..-22 dBFS peak under the voice (rules/audio.md); the mastering
    # step then lifts the whole mix, so this is a *relative* level.
    out["gain_db"] = _num(d.get("gain_db", -18.0), path + ".gain_db", -60.0, 6.0)
    out["duck"] = _duck(d.get("duck"), path + ".duck")
    out["fade_in"] = _num(d.get("fade_in", 0.5), path + ".fade_in", 0.0, 30.0)
    out["fade_out"] = _num(d.get("fade_out", 0.8), path + ".fade_out", 0.0, 30.0)
    out["offset"] = _num(d.get("offset", 0.0), path + ".offset", 0.0, 3600.0)  # seconds into the track to start from
    out["loop"] = _bool(d.get("loop", True), path + ".loop")
    return out


def _sfx(raw: Any, path: str, clip_ids: List[str]) -> Dict[str, Any]:
    d = _dict(raw, path, ("id", "asset", "file", "on", "gain_db", "align", "lead"))
    out = _asset_or_file(d, path)
    out["on"] = _anchor(d.get("on"), path + ".on", clip_ids)
    out["id"] = str(d.get("id", "sfx"))[:32]
    out["gain_db"] = _num(d.get("gain_db", -6.0), path + ".gain_db", -60.0, 12.0)
    # `peak` lands the sound's loudest instant on the picture (start = hit - lead -
    # peak offset); `start` plays the file from the anchor, for beds and risers.
    out["align"] = _choice(d.get("align", "peak"), path + ".align", ("peak", "start"))
    out["lead"] = _num(d.get("lead", 0.05), path + ".lead", 0.0, 0.5)
    return out


def _master(raw: Any, path: str) -> Dict[str, Any]:
    d = _dict(raw if raw is not None else {}, path,
              ("loudness_lufs", "true_peak_db", "lra", "limiter", "fade_in", "fade_out", "normalize"))
    return {
        # -14 LUFS is where YouTube lands a louder upload; mastering to it stops the
        # platform turning the video down. The measured reel this engine replaces
        # was -21.7 LUFS - about 8 dB under it.
        "loudness_lufs": _num(d.get("loudness_lufs", -14.0), path + ".loudness_lufs", -30.0, -5.0),
        "true_peak_db": _num(d.get("true_peak_db", -1.0), path + ".true_peak_db", -6.0, 0.0),
        "lra": _num(d.get("lra", 11.0), path + ".lra", 1.0, 30.0),
        "limiter": _bool(d.get("limiter", True), path + ".limiter"),
        "fade_in": _num(d.get("fade_in", 0.0), path + ".fade_in", 0.0, 10.0),
        "fade_out": _num(d.get("fade_out", 0.6), path + ".fade_out", 0.0, 10.0),
        "normalize": _bool(d.get("normalize", True), path + ".normalize"),
    }


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------


def _output(raw: Any, path: str) -> Dict[str, Any]:
    d = _dict(raw if raw is not None else {}, path,
              ("preset", "width", "height", "fps", "fit", "crf", "x264_preset", "audio_bitrate",
               "sample_rate", "bframes", "gop_seconds", "faststart", "preview"))
    # `faststart` and `preview` belong to the preset, not the author; they are accepted
    # (and ignored) only so a normalised spec written by `to_json` loads back unchanged
    preset = _choice(d.get("preset", "youtube_1080p60"), path + ".preset", tuple(OUTPUT_PRESETS))
    out = dict(OUTPUT_PRESETS[preset])
    out["preset"] = preset
    for key, lo, hi in (("width", 160, 7680), ("height", 90, 4320), ("fps", 10, 120), ("crf", 0, 51),
                        ("bframes", 0, 4), ("sample_rate", 8000, 96000)):
        if key in d:
            out[key] = _int(d[key], "{0}.{1}".format(path, key), lo, hi)
    if "gop_seconds" in d:
        out["gop_seconds"] = _num(d["gop_seconds"], path + ".gop_seconds", 0.1, 10.0)
    if "fit" in d:
        out["fit"] = _choice(d["fit"], path + ".fit", FITS)
    if "x264_preset" in d:
        out["x264_preset"] = _choice(d["x264_preset"], path + ".x264_preset", (
            "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"))
    if "audio_bitrate" in d:
        if not re.match(r"^\d{2,3}k$", str(d["audio_bitrate"])):
            _fail(path + ".audio_bitrate", "must look like 192k")
        out["audio_bitrate"] = str(d["audio_bitrate"])
    out["width"] -= out["width"] % 2
    out["height"] -= out["height"] % 2
    return out


def normalize(spec: Any) -> Dict[str, Any]:
    """Validate an edit spec and return it with every default filled in.

    Raises SpecError (a ValueError) with the path of the first bad field. The
    result is plain JSON-able data, safe to fingerprint: the renderer hashes it to
    decide which cached clips are still good.
    """
    d = _dict(spec, "spec", ("version", "name", "profile", "output", "sources", "clips", "tracks", "master",
                              "defaults", "notes"))
    version = d.get("version", VERSION)
    if version != VERSION:
        _fail("spec.version", "this build reads version {0}, the spec says {1}".format(VERSION, version))
    name = d.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        _fail("spec.name", "use 1-64 letters, digits, ., _ or - (it becomes the output folder)")
    profile = _choice(d.get("profile", "punchy"), "spec.profile", tuple(PROFILES))

    defaults = _dict(d.get("defaults", {}), "spec.defaults", ("transition",))
    default_trans = _num(defaults.get("transition", PROFILES[profile]["transition"]),
                         "spec.defaults.transition", 0.0, 3.0)

    sources: Dict[str, Any] = {}
    for alias, s in _dict(d.get("sources", {}), "spec.sources", tuple(d.get("sources", {}))).items():
        if not _ALIAS_RE.match(alias):
            _fail("spec.sources." + alias, "alias must be 1-32 letters, digits, _ or -")
        sd = _dict(s, "spec.sources." + alias, ("workspace", "file"))
        if ("workspace" in sd) == ("file" in sd):
            _fail("spec.sources." + alias, "give exactly one of `workspace` (a ClipBot slug) or `file`")
        if "workspace" in sd:
            ws = _str(sd["workspace"], "spec.sources.{0}.workspace".format(alias), 120)
            if not re.match(r"^[a-z0-9._-]{1,120}$", ws) or ws.startswith("_"):
                _fail("spec.sources.{0}.workspace".format(alias), "not a valid workspace slug")
            sources[alias] = {"workspace": ws}
        else:
            f = _str(sd["file"], "spec.sources.{0}.file".format(alias), 500)
            if not Path(f).is_absolute():
                _fail("spec.sources.{0}.file".format(alias), "must be an absolute path")
            sources[alias] = {"file": f}

    raw_clips = _list(d.get("clips"), "spec.clips", MAX_CLIPS)
    if not raw_clips:
        _fail("spec.clips", "an edit needs at least one clip")
    clips = [_clip(c, "spec.clips[{0}]".format(i), sources, default_trans) for i, c in enumerate(raw_clips)]
    ids = [c["id"] for c in clips]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        _fail("spec.clips", "duplicate clip id(s): {0}".format(", ".join(dupes)))
    # the first clip has nothing to transition from
    clips[0]["transition_in"] = {"type": "cut", "dur": 0.0}

    tracks = _dict(d.get("tracks", {}), "spec.tracks", ("music", "sfx"))
    norm_tracks = {
        "music": [_music(m, "spec.tracks.music[{0}]".format(i), ids)
                  for i, m in enumerate(_list(tracks.get("music", []), "spec.tracks.music", 8))],
        "sfx": [_sfx(s, "spec.tracks.sfx[{0}]".format(i), ids)
                for i, s in enumerate(_list(tracks.get("sfx", []), "spec.tracks.sfx", 400))],
    }

    return {
        "version": VERSION,
        "name": name,
        "profile": profile,
        "notes": str(d.get("notes", ""))[:2000],
        "output": _output(d.get("output"), "spec.output"),
        "sources": sources,
        "defaults": {"transition": default_trans},
        "clips": clips,
        "tracks": norm_tracks,
        "master": _master(d.get("master"), "spec.master"),
    }


def load(path: Any) -> Dict[str, Any]:
    """Read and validate a spec file (utf-8-sig, so a Notepad BOM is harmless)."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SpecError("{0}: no such file".format(p))
    except ValueError as exc:
        raise SpecError("{0}: not valid JSON ({1})".format(p, exc))
    return normalize(data)


def to_json(spec: Dict[str, Any]) -> str:
    return json.dumps(spec, indent=1, sort_keys=False, ensure_ascii=False)
