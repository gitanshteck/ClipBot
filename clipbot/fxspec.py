"""Clip effects: validation, timing resolution, and ffmpeg filter fragments.

Pure math and string building - no IO, no ffmpeg, no web framework, exactly
like `reelspec.py`, and for the same reason: the dashboard resolves effects to
show the FX strip and its warnings without touching disk, while the renderer
resolves the same effects to build a command. One implementation, so the two
cannot drift.

Asset metadata (a sound's duration, a sticker's pixel size, a font's path) is
*injected* by the caller as plain dicts. This module never opens a file and
never learns where the library lives; `clipbot/library.py` owns all of that.

Time base
---------
An effect's `at` is an **absolute VOD timestamp** - the same clock as
`clip["start"]`, the video element's `currentTime` and both dashboard
timelines. It is not relative to the clip.

That choice is what makes an effect stay glued to the moment in the stream it
was authored against. Storing times relative to the padded clip start would
mean editing the global `cut.pad_start` silently slid every effect in every
clip in every workspace; storing them relative to the raw clip start would
survive nudging the out point but not the in point, so trimming a second of
dead air off the front would walk the airhorn off the headshot.

Presets are the one thing stored relatively (an `offset` from an anchor), which
is right: a preset is portable by definition, a placed effect is not.

Escaping
--------
Every expression-valued filter option is wrapped in single quotes. That is not
cosmetic: `between(t,1,2)` contains commas, and an unquoted value ends at the
first comma, so the graph silently splits into two filters and ffmpeg reports a
parse error that points somewhere else entirely.

Asset paths reach ffmpeg as argv entries (`-i <path>`), never inside the
filtergraph, so they need no escaping at all. The single exception is
`drawtext`'s `fontfile=`, which has nowhere else to live - see `escape_path`.
"""

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .specerror import SpecError

# Effects that place themselves on the timeline, versus those that apply to the
# whole clip. `freeze` and `speed` are whole-clip in v1 - see the module notes
# in build_tail_chains for why a mid-clip one is a different feature.
TIMED_TYPES = ("punch", "shake", "flash", "sfx", "sticker", "text")
WHOLE_TYPES = ("freeze", "speed", "music")
TYPES = TIMED_TYPES + WHOLE_TYPES

SINGLETON_TYPES = ("speed", "music")

MAX_EFFECTS = 24

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$|^[a-zA-Z]{3,20}(@[01](\.\d+)?)?$")

# Defaults live here rather than being read from settings, because this module
# is not allowed to know about settings. The dashboard reads `reel.fx.*` when it
# *creates* an effect, so a user-tuned default still wins in practice; these are
# the floor for a spec that arrived without a field (a hand-edited clips.json,
# or an older preset).
DEFAULTS = {
    "punch": {"dur": 0.45, "amount": 1.18, "ease": "step"},
    "shake": {"dur": 0.60, "amount": 8.0, "freq": 12.0, "decay": True},
    "flash": {"dur": 0.12, "amount": 0.55, "style": "exposure"},
    "freeze": {"dur": 0.80},
    "speed": {"rate": 1.0},
    "sfx": {"gain_db": -3.0},
    "music": {"gain_db": -18.0},
    "sticker": {"dur": 2.0, "w": 0.30, "opacity": 1.0, "fade": 0.15,
                "x": 0.5, "y": 0.5},
    "text": {"dur": 3.0, "x": 0.5, "y": 0.14},
}

RANGES = {
    "punch.amount": (1.02, 2.0),
    "punch.dur": (0.05, 5.0),
    "shake.amount": (1.0, 40.0),
    "shake.freq": (1.0, 30.0),
    "shake.dur": (0.05, 5.0),
    "flash.amount": (0.05, 1.0),
    "flash.dur": (0.02, 2.0),
    "freeze.dur": (0.05, 3.0),
    # atempo's own single-instance range. Going outside it would need the filter
    # chained with itself, which is a different (and audibly worse) feature.
    "speed.rate": (0.5, 2.0),
    "gain_db": (-40.0, 12.0),
    "sticker.w": (0.02, 1.0),
    "sticker.dur": (0.05, 60.0),
    "sticker.fade": (0.0, 2.0),
    "sticker.opacity": (0.05, 1.0),
    "text.dur": (0.05, 60.0),
    "text.size": (0.01, 0.30),
}

MAX_TEXT_CHARS = 120

# Default text look, used when an effect names no style and the library has none.
DEFAULT_TEXT_STYLE = {
    "font": None,
    "size": 0.055,
    "color": "#FFFFFF",
    "border_w": 6,
    "border_color": "black@0.9",
    "shadow": [3, 3],
    "uppercase": True,
}

DEFAULT_DUCK = {
    "enabled": True,
    "threshold": 0.05,
    "ratio": 8.0,
    "attack": 20.0,
    "release": 300.0,
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _even(n: float) -> int:
    i = int(round(n))
    return i - (i % 2)


def _num(value: Any, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise SpecError("{0} must be a number, got {1!r}".format(name, value))
    if math.isnan(f) or math.isinf(f):
        raise SpecError("{0} must be a finite number".format(name))
    return f


def _ranged(value: Any, key: str, name: str) -> float:
    lo, hi = RANGES[key]
    f = _num(value, name)
    if not (lo <= f <= hi):
        raise SpecError(
            "{0} must be between {1} and {2}, got {3}".format(name, lo, hi, f)
        )
    return round(f, 4)


def _frac(value: Any, name: str) -> float:
    f = _num(value, name)
    if not (0.0 <= f <= 1.0):
        raise SpecError("{0} must be between 0 and 1, got {1}".format(name, f))
    return round(f, 4)


def _asset_id(value: Any, name: str) -> str:
    text = str(value or "")
    if not re.match(r"^[a-z]{3}_[0-9a-f]{8,32}$", text):
        raise SpecError("{0} must be a library asset id, got {1!r}".format(name, value))
    return text


def _color(value: Any, name: str, default: str) -> str:
    text = str(value or default)
    if not _COLOR_RE.match(text):
        raise SpecError(
            "{0} must be #RRGGBB or an ffmpeg colour name, got {1!r}".format(name, value)
        )
    return text


def _q(expr: str) -> str:
    """Single-quote a filter expression. See the module docstring."""
    if "'" in expr:  # we generate these, so this is an internal-error guard
        raise SpecError("generated expression contains a quote: {0}".format(expr))
    return "'" + expr + "'"


def _between(a: float, b: float) -> str:
    return "between(t,{0:.3f},{1:.3f})".format(a, b)


def escape_path(path: Any) -> str:
    """Quote a path for use *inside* a filter option (only drawtext's fontfile).

    Backslashes become forward slashes, which ffmpeg accepts on Windows, and
    the drive colon is escaped so the filter parser does not read it as an
    option separator. Because commands are built as argv and never handed to a
    shell, exactly one level of escaping applies - the doubled-backslash recipes
    that circulate online are for shell invocation and produce a broken path
    here.
    """
    text = str(path).replace("\\", "/")
    if "'" in text:
        raise SpecError(
            "font path contains a quote, which ffmpeg's filter parser cannot "
            "escape: {0}".format(text)
        )
    return "'" + text.replace(":", r"\:") + "'"


def escape_text(text: str) -> str:
    """Escape a drawtext literal. Callers must have rejected `'` already."""
    out = str(text)
    out = out.replace("\\", r"\\")
    out = out.replace(":", r"\:")
    out = out.replace("%", r"\%")
    out = out.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")
    return out


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def normalize_fx(fx: Any, max_effects: int = MAX_EFFECTS) -> List[Dict[str, Any]]:
    """Validate an effects list. Returns [] for None/empty.

    Raises SpecError (a ValueError) on anything invalid, which the PATCH route
    turns into a 400 - so a bad effect reaches the user as a message rather
    than as a render that quietly does nothing.
    """
    if fx is None or fx == []:
        return []
    if not isinstance(fx, list):
        raise SpecError("fx must be a list of effects")
    if len(fx) > max_effects:
        raise SpecError(
            "too many effects: {0} (limit {1})".format(len(fx), max_effects)
        )

    out: List[Dict[str, Any]] = []
    seen_ids = set()
    seen_singletons = set()
    for index, raw in enumerate(fx):
        name = "fx[{0}]".format(index)
        if not isinstance(raw, dict):
            raise SpecError("{0} must be an object".format(name))
        kind = raw.get("type")
        if kind not in TYPES:
            raise SpecError(
                "{0}.type must be one of {1}, got {2!r}".format(
                    name, ", ".join(TYPES), kind
                )
            )
        if kind in SINGLETON_TYPES:
            if kind in seen_singletons:
                raise SpecError(
                    "only one {0} effect is allowed per clip - a second one "
                    "would silently win over the first".format(kind)
                )
            seen_singletons.add(kind)

        eff_id = str(raw.get("id") or "")
        if not _ID_RE.match(eff_id):
            raise SpecError(
                "{0}.id must be 1-24 characters of A-Z a-z 0-9 _ -".format(name)
            )
        if eff_id in seen_ids:
            raise SpecError("duplicate effect id {0!r}".format(eff_id))
        seen_ids.add(eff_id)

        eff = {"id": eff_id, "type": kind}
        if not raw.get("enabled", True):
            # Kept rather than dropped: this is how a user A/Bs an effect, and
            # how a preset whose sound is missing from the library still lands.
            eff["enabled"] = False

        if kind in TIMED_TYPES:
            eff["at"] = round(_num(raw.get("at"), name + ".at"), 3)
            if eff["at"] < 0:
                raise SpecError("{0}.at must not be negative".format(name))

        out.append(_normalize_one(kind, raw, eff, name))

    return out


def _normalize_one(kind, raw, eff, name):
    d = DEFAULTS[kind]

    if kind == "punch":
        eff["dur"] = _ranged(raw.get("dur", d["dur"]), "punch.dur", name + ".dur")
        eff["amount"] = _ranged(
            raw.get("amount", d["amount"]), "punch.amount", name + ".amount"
        )
        ease = raw.get("ease", d["ease"])
        if ease not in ("step", "smooth"):
            raise SpecError("{0}.ease must be 'step' or 'smooth'".format(name))
        eff["ease"] = ease

    elif kind == "shake":
        eff["dur"] = _ranged(raw.get("dur", d["dur"]), "shake.dur", name + ".dur")
        eff["amount"] = _ranged(
            raw.get("amount", d["amount"]), "shake.amount", name + ".amount"
        )
        eff["freq"] = _ranged(raw.get("freq", d["freq"]), "shake.freq", name + ".freq")
        eff["decay"] = bool(raw.get("decay", d["decay"]))

    elif kind == "flash":
        eff["dur"] = _ranged(raw.get("dur", d["dur"]), "flash.dur", name + ".dur")
        eff["amount"] = _ranged(
            raw.get("amount", d["amount"]), "flash.amount", name + ".amount"
        )
        style = raw.get("style", d["style"])
        if style not in ("exposure", "white"):
            raise SpecError("{0}.style must be 'exposure' or 'white'".format(name))
        eff["style"] = style

    elif kind == "freeze":
        eff["dur"] = _ranged(raw.get("dur", d["dur"]), "freeze.dur", name + ".dur")

    elif kind == "speed":
        eff["rate"] = _ranged(raw.get("rate", d["rate"]), "speed.rate", name + ".rate")

    elif kind == "sfx":
        eff["asset"] = _asset_id(raw.get("asset"), name + ".asset")
        eff["gain_db"] = _ranged(
            raw.get("gain_db", d["gain_db"]), "gain_db", name + ".gain_db"
        )

    elif kind == "music":
        eff["asset"] = _asset_id(raw.get("asset"), name + ".asset")
        eff["gain_db"] = _ranged(
            raw.get("gain_db", d["gain_db"]), "gain_db", name + ".gain_db"
        )
        eff["duck"] = _duck(raw.get("duck"), name + ".duck")

    elif kind == "sticker":
        eff["asset"] = _asset_id(raw.get("asset"), name + ".asset")
        eff["dur"] = _ranged(raw.get("dur", d["dur"]), "sticker.dur", name + ".dur")
        eff["w"] = _ranged(raw.get("w", d["w"]), "sticker.w", name + ".w")
        eff["x"] = _frac(raw.get("x", d["x"]), name + ".x")
        eff["y"] = _frac(raw.get("y", d["y"]), name + ".y")
        eff["opacity"] = _ranged(
            raw.get("opacity", d["opacity"]), "sticker.opacity", name + ".opacity"
        )
        eff["fade"] = _ranged(
            raw.get("fade", d["fade"]), "sticker.fade", name + ".fade"
        )

    elif kind == "text":
        eff["text"] = _text(raw.get("text"), name + ".text")
        eff["dur"] = _ranged(raw.get("dur", d["dur"]), "text.dur", name + ".dur")
        eff["x"] = _frac(raw.get("x", d["x"]), name + ".x")
        eff["y"] = _frac(raw.get("y", d["y"]), name + ".y")
        style = raw.get("style")
        if style is None or isinstance(style, str):
            if style:
                if not _ID_RE.match(style):
                    raise SpecError("{0}.style is not a valid style name".format(name))
                eff["style"] = style
        elif isinstance(style, dict):
            eff["style"] = normalize_text_style(style, name + ".style")
        else:
            raise SpecError("{0}.style must be a style name or an object".format(name))

    return eff


def _text(value, name):
    text = value if isinstance(value, str) else ""
    if not text.strip():
        raise SpecError("{0} must not be empty".format(name))
    if len(text) > MAX_TEXT_CHARS:
        raise SpecError(
            "{0} is {1} characters, limit is {2}".format(name, len(text), MAX_TEXT_CHARS)
        )
    if "'" in text:
        raise SpecError(
            "{0} cannot contain a straight apostrophe - ffmpeg's drawtext has no "
            "way to escape one inside a quoted value. Use a typographic "
            "apostrophe (’) instead.".format(name)
        )
    for ch in text:
        if ord(ch) < 32 and ch not in "\r\n":
            raise SpecError("{0} contains a control character".format(name))
    return text


def normalize_text_style(style: Any, name: str = "style") -> Dict[str, Any]:
    """Validate a text style, inline or from the library's textstyles.json."""
    if not isinstance(style, dict):
        raise SpecError("{0} must be an object".format(name))
    out = dict(DEFAULT_TEXT_STYLE)
    if style.get("font"):
        out["font"] = _asset_id(style["font"], name + ".font")
    if "size" in style:
        out["size"] = _ranged(style["size"], "text.size", name + ".size")
    if "color" in style:
        out["color"] = _color(style["color"], name + ".color", "#FFFFFF")
    if "border_w" in style:
        bw = _num(style["border_w"], name + ".border_w")
        if not (0 <= bw <= 40):
            raise SpecError("{0}.border_w must be between 0 and 40".format(name))
        out["border_w"] = int(bw)
    if "border_color" in style:
        out["border_color"] = _color(
            style["border_color"], name + ".border_color", "black@0.9"
        )
    if "shadow" in style:
        sh = style["shadow"] or [0, 0]
        if not isinstance(sh, (list, tuple)) or len(sh) != 2:
            raise SpecError("{0}.shadow must be [x, y]".format(name))
        out["shadow"] = [int(_num(sh[0], name + ".shadow[0]")),
                         int(_num(sh[1], name + ".shadow[1]"))]
    if "uppercase" in style:
        out["uppercase"] = bool(style["uppercase"])
    return out


def _duck(duck, name):
    if duck is None:
        return dict(DEFAULT_DUCK)
    if not isinstance(duck, dict):
        raise SpecError("{0} must be an object".format(name))
    out = dict(DEFAULT_DUCK)
    out["enabled"] = bool(duck.get("enabled", True))
    if "threshold" in duck:
        t = _num(duck["threshold"], name + ".threshold")
        if not (0.001 <= t <= 1.0):
            raise SpecError("{0}.threshold must be between 0.001 and 1".format(name))
        out["threshold"] = round(t, 4)
    if "ratio" in duck:
        r = _num(duck["ratio"], name + ".ratio")
        if not (1.0 <= r <= 20.0):
            raise SpecError("{0}.ratio must be between 1 and 20".format(name))
        out["ratio"] = round(r, 2)
    for key, lo, hi in (("attack", 0.01, 2000.0), ("release", 0.01, 9000.0)):
        if key in duck:
            v = _num(duck[key], "{0}.{1}".format(name, key))
            if not (lo <= v <= hi):
                raise SpecError(
                    "{0}.{1} must be between {2} and {3} ms".format(name, key, lo, hi)
                )
            out[key] = round(v, 2)
    return out


# --------------------------------------------------------------------------
# resolution: absolute VOD time -> local clip time, ids -> assets
# --------------------------------------------------------------------------


def resolve_fx(
    fx: Any,
    origin: float,
    span: float,
    canvas: Tuple[int, int],
    assets: Optional[Dict[str, Dict[str, Any]]] = None,
    styles: Optional[Dict[str, Dict[str, Any]]] = None,
    next_input: int = 1,
    strict: bool = True,
    fallback_font: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Turn a validated effects list into a render-ready plan.

    `origin` is the absolute VOD time that maps to local t=0 - the padded clip
    start for a render, or the preview window start for a proxy. `span` is that
    window's length in source seconds.

    `assets` maps asset id -> {path, kind, duration, width, height, animated}.
    `next_input` is the ffmpeg input index the first extra `-i` will take (1
    with no chat layer, 2 with one).

    `strict` decides what a missing asset means. The renderer passes True, so a
    clip referencing a deleted sound fails loudly with the filename rather than
    quietly rendering without it. The dashboard's /reel/plan passes False, so
    the FX strip can show "missing" while you keep editing.

    `fallback_font` matters more than it looks. drawtext with no `fontfile=`
    asks fontconfig for a default, and the Windows ffmpeg builds ship without a
    fontconfig configuration - the render still exits 0, having drawn the text
    in whatever fontconfig guessed, or nothing at all. Passing a real path is
    what makes text deterministic.

    Returns None when nothing survives, which is the signal for callers to take
    the frozen effects-free path.
    """
    fx = normalize_fx(fx)
    if not fx:
        return None
    assets = assets or {}
    styles = styles or {}
    cw, ch = int(canvas[0]), int(canvas[1])

    warnings: List[str] = []
    video: List[Dict[str, Any]] = []
    audio: List[Dict[str, Any]] = []
    inputs: List[Dict[str, Any]] = []
    hold = 0.0
    rate = 1.0
    index = int(next_input)

    for eff in fx:
        if eff.get("enabled") is False:
            continue
        kind = eff["type"]

        if kind == "freeze":
            hold += eff["dur"]
            continue
        if kind == "speed":
            rate = eff["rate"]
            continue

        asset = None
        if kind in ("sfx", "music", "sticker"):
            asset = assets.get(eff["asset"])
            if asset is None:
                message = "{0}: asset {1} is not in the library".format(
                    eff["id"], eff["asset"]
                )
                if strict:
                    raise SpecError(
                        message + " - run `python -m clipbot library scan`, or "
                        "remove the effect"
                    )
                warnings.append(message)
                continue

        if kind == "music":
            entry = _resolve_music(eff, asset, span, index)
            inputs.append(entry.pop("input"))
            index += 1
            audio.append(entry)
            continue

        # Everything left is timed. Rebase onto the window and drop what has
        # fallen outside it - nudging a clip's in/out point is routine, so this
        # is a warning, not an error.
        local = round(eff["at"] - origin, 3)
        dur = float(eff.get("dur", 0.0))
        if local >= span or local + dur <= 0:
            warnings.append(
                "{0} ({1}) falls outside the clip and will not render".format(
                    eff["id"], kind
                )
            )
            continue

        if kind == "sfx":
            if local < 0:
                # A half-played impact is worse than a missing one.
                warnings.append(
                    "{0} (sfx) starts before the clip and was dropped".format(eff["id"])
                )
                continue
            entry = _resolve_sfx(eff, asset, span, local, index)
            inputs.append(entry.pop("input"))
            index += 1
            audio.append(entry)
            continue

        start = max(0.0, local)
        end = min(span, local + dur) if dur else span
        if end - start < 0.01:
            warnings.append(
                "{0} ({1}) is clipped to nothing by the clip bounds".format(
                    eff["id"], kind
                )
            )
            continue

        entry = {"id": eff["id"], "type": kind, "start": round(start, 3),
                 "end": round(end, 3), "clipped": (start, end) != (local, local + dur)}

        if kind == "punch":
            entry["amount"] = eff["amount"]
            entry["ease"] = eff["ease"]
        elif kind == "shake":
            # Amplitude is authored against a 1080-wide canvas so the same spec
            # shakes by the same *proportion* at 360x640 in the proxy preview.
            entry["amount_px"] = eff["amount"] * (cw / 1080.0)
            entry["freq"] = eff["freq"]
            entry["decay"] = eff["decay"]
        elif kind == "flash":
            entry["amount"] = eff["amount"]
            entry["style"] = eff["style"]
        elif kind == "sticker":
            entry.update(_resolve_sticker(eff, asset, cw, ch, index))
            inputs.append(entry.pop("input"))
            index += 1
        elif kind == "text":
            resolved, style_warn = _resolve_text(
                eff, styles, assets, cw, ch, fallback_font
            )
            entry.update(resolved)
            if style_warn:
                warnings.append(style_warn)

        video.append(entry)

    if not video and not audio and not hold and rate == 1.0:
        return None

    return {
        "video": video,
        "audio": audio,
        "inputs": inputs,
        "hold": round(hold, 3),
        "rate": rate,
        "canvas": [cw, ch],
        "span": round(span, 3),
        # A hold lengthens the output and a speed change shortens it. This is
        # what -t must use; see reelspec.build_argv.
        "out_duration": round((span + hold) / rate, 3),
        "warnings": warnings,
    }


def _resolve_sticker(eff, asset, cw, ch, index):
    aw = int(asset.get("width") or 0)
    ah = int(asset.get("height") or 0)
    if aw <= 0 or ah <= 0:
        raise SpecError(
            "{0}: sticker {1} has no known pixel size - rescan the "
            "library".format(eff["id"], asset.get("name") or eff["asset"])
        )
    dst_w = max(2, _even(cw * eff["w"]))
    dst_h = max(2, _even(dst_w * (ah / float(aw))))
    x = max(0, min(_even(cw * eff["x"] - dst_w / 2.0), cw - dst_w))
    y = max(0, min(_even(ch * eff["y"] - dst_h / 2.0), ch - dst_h))

    animated = bool(asset.get("animated"))
    args = ["-stream_loop", "-1"] if animated else ["-loop", "1"]
    return {
        "w": dst_w, "h": dst_h, "x": x, "y": y,
        "opacity": eff["opacity"], "fade": eff["fade"], "index": index,
        "input": {"role": "sticker", "index": index, "fx_id": eff["id"],
                  "args": args + ["-i", str(asset["path"])]},
    }


def _resolve_text(eff, styles, assets, cw, ch, fallback_font):
    style = eff.get("style")
    warning = None
    if isinstance(style, str):
        found = styles.get(style)
        if found is None:
            # Not fatal: a missing style still renders legible text, and failing
            # a whole reel over a look would be the wrong trade.
            warning = "{0}: text style {1!r} is not in the library, using the " \
                      "default look".format(eff["id"], style)
            style = dict(DEFAULT_TEXT_STYLE)
        else:
            style = normalize_text_style(found, "textstyles." + str(style))
    elif not isinstance(style, dict):
        style = dict(DEFAULT_TEXT_STYLE)

    fontfile = None
    if style.get("font"):
        asset = assets.get(style["font"])
        if asset is None:
            warning = "{0}: the style's font is not in the library".format(eff["id"])
        else:
            fontfile = str(asset["path"])
    if not fontfile:
        fontfile = fallback_font or None
    if not fontfile:
        warning = warning or (
            "{0}: no font available, so ffmpeg will ask fontconfig for one - on "
            "Windows that usually means the text does not appear".format(eff["id"])
        )

    text = eff["text"]
    if style.get("uppercase"):
        text = text.upper()

    return {
        "text": text,
        "fontfile": fontfile,
        "size_px": max(8, int(round(ch * float(style["size"])))),
        "color": style["color"],
        "border_w": int(style["border_w"]),
        "border_color": style["border_color"],
        "shadow": list(style["shadow"]),
        "x_px": int(round(cw * eff["x"])),
        "y_px": int(round(ch * eff["y"])),
    }, warning


def _resolve_sfx(eff, asset, span, local, index):
    dur = float(asset.get("duration") or 0.0)
    if dur <= 0:
        raise SpecError(
            "{0}: sound {1} has no known duration - rescan the library".format(
                eff["id"], asset.get("name") or eff["asset"]
            )
        )
    return {
        "id": eff["id"], "type": "sfx", "index": index,
        "at": round(local, 3),
        "take": round(min(dur, max(0.05, span - local)), 3),
        "gain_db": eff["gain_db"],
        "input": {"role": "sfx", "index": index, "fx_id": eff["id"],
                  "args": ["-i", str(asset["path"])]},
    }


def _resolve_music(eff, asset, span, index):
    return {
        "id": eff["id"], "type": "music", "index": index,
        "take": round(span, 3),
        "gain_db": eff["gain_db"],
        "duck": eff["duck"],
        # -stream_loop -1 so a 30-second bed covers a 90-second clip. The
        # atrim in the filter is what actually bounds it.
        "input": {"role": "music", "index": index, "fx_id": eff["id"],
                  "args": ["-stream_loop", "-1", "-i", str(asset["path"])]},
    }


# --------------------------------------------------------------------------
# filter fragments
# --------------------------------------------------------------------------


def build_camera_chains(fx_plan, label_in, w, h):
    """Punch and shake: whole-frame camera moves, applied to the composed video.

    Run before the chat layer and before any overlay, so a punch-in zooms the
    gameplay and not the chat box or the sticker.
    """
    chains: List[str] = []
    label = label_in
    if not fx_plan:
        return chains, label

    for eff in fx_plan["video"]:
        if eff["type"] != "punch":
            continue
        amount = eff["amount"]
        zw, zh = max(w + 2, _even(w * amount)), max(h + 2, _even(h * amount))
        ox, oy = _even((zw - w) / 2.0), _even((zh - h) / 2.0)
        tag = "pz_" + eff["id"]
        # split + pre-scaled overlay rather than zoompan: zoompan needs an
        # explicit fps we do not record anywhere, and it re-times its output, so
        # a wrong guess silently drifts the clip's duration. A step punch is
        # also what a "punch-in" reads as.
        chains.append("[{0}]split=2[{1}a][{1}b]".format(label, tag))
        chains.append(
            "[{0}b]scale={1}:{2}:flags=bilinear,crop={3}:{4}:{5}:{6}[{0}c]".format(
                tag, zw, zh, w, h, ox, oy
            )
        )
        chains.append(
            "[{0}a][{0}c]overlay=x=0:y=0:enable={1}[{0}o]".format(
                tag, _q(_between(eff["start"], eff["end"]))
            )
        )
        label = tag + "o"

    shakes = [e for e in fx_plan["video"] if e["type"] == "shake"]
    if shakes:
        # One crop node for every shake, not one per shake: each would cost
        # another scale+crop resample, and overlapping shakes should add rather
        # than compound through repeated rescaling.
        margin = _even(max(e["amount_px"] for e in shakes) + 2) + 2
        terms_x, terms_y = [], []
        for eff in shakes:
            a, b = eff["start"], eff["end"]
            span = max(0.001, b - a)
            decay = "*(1-(t-{0:.3f})/{1:.3f})".format(a, span) if eff["decay"] else ""
            amp = eff["amount_px"]
            terms_x.append(
                "if({0},{1:.2f}*sin(6.2832*{2:.2f}*(t-{3:.3f})){4},0)".format(
                    _between(a, b), amp, eff["freq"], a, decay
                )
            )
            terms_y.append(
                "if({0},{1:.2f}*cos(6.2832*{2:.2f}*(t-{3:.3f})){4},0)".format(
                    _between(a, b), amp * 0.8, eff["freq"] * 1.3, a, decay
                )
            )
        # crop's x/y are evaluated per frame; its w/h are not, which is exactly
        # why the shake is a moving crop window over a slightly enlarged frame
        # rather than an animated scale.
        chains.append(
            "[{0}]scale={1}:{2}:flags=bilinear,crop={3}:{4}:x={5}:y={6}[fxshake]".format(
                label, w + 2 * margin, h + 2 * margin, w, h,
                _q("{0}+{1}".format(margin, "+".join(terms_x))),
                _q("{0}+{1}".format(margin, "+".join(terms_y))),
            )
        )
        label = "fxshake"

    return chains, label


def build_overlay_chains(fx_plan, label_in, w, h):
    """Stickers then text, both at full canvas size and above the chat layer."""
    chains: List[str] = []
    label = label_in
    if not fx_plan:
        return chains, label

    for eff in fx_plan["video"]:
        if eff["type"] != "sticker":
            continue
        tag = "stk_" + eff["id"]
        start, end = eff["start"], eff["end"]
        parts = ["[{0}:v]scale={1}:{2}:flags=lanczos,format=rgba".format(
            eff["index"], eff["w"], eff["h"])]
        if eff["opacity"] < 1.0:
            parts.append("colorchannelmixer=aa={0:.3f}".format(eff["opacity"]))
        # Shift the sticker's own timeline so its t=0 lands at `start`; the
        # fades below and the overlay's enable= then all speak the same clock.
        parts.append("setpts=PTS-STARTPTS+{0:.3f}/TB".format(start))
        fade = eff["fade"]
        if fade > 0 and (end - start) > 2 * fade:
            parts.append("fade=t=in:st={0:.3f}:d={1:.3f}:alpha=1".format(start, fade))
            parts.append(
                "fade=t=out:st={0:.3f}:d={1:.3f}:alpha=1".format(end - fade, fade)
            )
        chains.append(",".join(parts) + "[{0}]".format(tag))
        chains.append(
            "[{0}][{1}]overlay=x={2}:y={3}:enable={4}:format=auto[{1}o]".format(
                label, tag, eff["x"], eff["y"], _q(_between(start, end))
            )
        )
        label = tag + "o"

    for eff in fx_plan["video"]:
        if eff["type"] != "text":
            continue
        tag = "txt_" + eff["id"]
        opts = []
        if eff.get("fontfile"):
            opts.append("fontfile=" + escape_path(eff["fontfile"]))
        opts.append("text='" + escape_text(eff["text"]) + "'")
        opts.append("fontsize={0}".format(eff["size_px"]))
        opts.append("fontcolor={0}".format(eff["color"]))
        if eff["border_w"]:
            opts.append("borderw={0}".format(eff["border_w"]))
            opts.append("bordercolor={0}".format(eff["border_color"]))
        sx, sy = eff["shadow"]
        if sx or sy:
            opts.append("shadowx={0}:shadowy={1}".format(sx, sy))
        # Clamped rather than plain centring: text_w is only known to ffmpeg at
        # draw time, so a long hook at a small canvas would otherwise centre
        # itself off both edges and lose its first and last words.
        opts.append("x=" + _q(
            "max(8,min({0}-text_w/2,w-text_w-8))".format(eff["x_px"])
        ))
        opts.append("y=" + _q(
            "max(8,min({0}-text_h/2,h-text_h-8))".format(eff["y_px"])
        ))
        opts.append("enable=" + _q(_between(eff["start"], eff["end"])))
        chains.append("[{0}]drawtext={1}[{2}]".format(label, ":".join(opts), tag))
        label = tag

    return chains, label


def build_tail_chains(fx_plan, label_in):
    """Flash, then the freeze hold, then speed.

    Flash sits above everything visible: a flash the title punches through is
    not a flash. The hold comes after so the held frame carries every layer,
    and speed is last so every effect above it could be authored in ordinary
    source time - the alternative, applying speed first, would mean dividing
    every other effect's timing by the rate.
    """
    chains: List[str] = []
    label = label_in
    if not fx_plan:
        return chains, label

    exposures = [e for e in fx_plan["video"]
                 if e["type"] == "flash" and e["style"] == "exposure"]
    if exposures:
        terms = []
        for eff in exposures:
            a, b = eff["start"], eff["end"]
            span = max(0.001, b - a)
            terms.append(
                "if({0},{1:.3f}*(1-(t-{2:.3f})/{3:.3f}),0)".format(
                    _between(a, b), eff["amount"], a, span
                )
            )
        # eq's brightness takes an expression only with eval=frame; without it
        # the expression is evaluated once at init and the flash never fires.
        chains.append(
            "[{0}]eq=brightness={1}:eval=frame[fxflash]".format(
                label, _q("+".join(terms))
            )
        )
        label = "fxflash"

    for eff in fx_plan["video"]:
        if eff["type"] != "flash" or eff["style"] != "white":
            continue
        tag = "fw_" + eff["id"]
        # drawbox's colour alpha takes no expression, so this style is a hard
        # cut rather than a decay - which is why `exposure` is the default.
        chains.append(
            "[{0}]drawbox=x=0:y=0:w=iw:h=ih:color=white@{1:.3f}:t=fill:"
            "enable={2}[{3}]".format(
                label, eff["amount"], _q(_between(eff["start"], eff["end"])), tag
            )
        )
        label = tag

    if fx_plan["hold"] > 0:
        chains.append(
            "[{0}]tpad=stop_mode=clone:stop_duration={1:.3f}[fxhold]".format(
                label, fx_plan["hold"]
            )
        )
        label = "fxhold"

    if fx_plan["rate"] != 1.0:
        chains.append(
            "[{0}]setpts=PTS/{1:.4f}[fxspeed]".format(label, fx_plan["rate"])
        )
        label = "fxspeed"

    return chains, label


def build_audio_chains(fx_plan, has_source_audio=True, loudnorm=False):
    """The audio half of the graph. Returns (chains, map_label or None).

    Returns no chains at all when there is nothing to mix and no re-timing to
    do, so a video-only effect leaves the audio path exactly as it was.
    """
    if not fx_plan:
        return [], None

    beds = [e for e in fx_plan["audio"] if e["type"] == "music"]
    sfx = [e for e in fx_plan["audio"] if e["type"] == "sfx"]
    hold, rate = fx_plan["hold"], fx_plan["rate"]

    if not beds and not sfx and not hold and rate == 1.0:
        return [], None
    if not has_source_audio and not beds and not sfx:
        return [], None

    chains: List[str] = []
    mix_labels: List[str] = []
    key_label = None

    if has_source_audio:
        duck_wanted = any(b["duck"]["enabled"] for b in beds)
        if duck_wanted:
            chains.append("[0:a]asplit=2[amain][akey]")
            mix_labels.append("amain")
            key_label = "akey"
        else:
            mix_labels.append("0:a")

    for bed in beds:
        tag = "bed_" + bed["id"]
        chains.append(
            "[{0}:a]volume={1:.2f}dB,atrim=0:{2:.3f},asetpts=PTS-STARTPTS[{3}]".format(
                bed["index"], bed["gain_db"], bed["take"], tag
            )
        )
        duck = bed["duck"]
        if duck["enabled"] and key_label:
            chains.append(
                "[{0}][{1}]sidechaincompress=threshold={2}:ratio={3}:attack={4}"
                ":release={5}:level_sc=1[{0}d]".format(
                    tag, key_label, duck["threshold"], duck["ratio"],
                    duck["attack"], duck["release"],
                )
            )
            tag = tag + "d"
            key_label = None  # one sidechain consumer; a second bed rides unducked
        mix_labels.append(tag)

    for eff in sfx:
        tag = "sx_" + eff["id"]
        chains.append(
            "[{0}:a]atrim=0:{1:.3f},asetpts=PTS-STARTPTS,volume={2:.2f}dB,"
            # all=1 delays every channel without us having to know how many
            # there are - a mono sound effect and a stereo one both work.
            "adelay={3}:all=1[{4}]".format(
                eff["index"], eff["take"], eff["gain_db"],
                int(round(eff["at"] * 1000)), tag,
            )
        )
        mix_labels.append(tag)

    label = mix_labels[0]
    if len(mix_labels) > 1:
        chains.append(
            # normalize=0 is load-bearing. amix divides by the input count by
            # default, so adding a bed and two sound effects would silently drop
            # the stream audio by around 10 dB - the classic bug in this exact
            # feature. duration=first keeps the mix bounded by the stream audio.
            "[{0}]amix=inputs={1}:duration={2}:dropout_transition=0:normalize=0"
            "[amix]".format(
                "][".join(mix_labels), len(mix_labels),
                "first" if has_source_audio else "longest",
            )
        )
        label = "amix"

    if loudnorm:
        # After the mix, which is semantically where `-af loudnorm` already ran.
        chains.append("[{0}]loudnorm=I=-14:TP=-1.5:LRA=11[aln]".format(label))
        label = "aln"

    if hold > 0:
        chains.append("[{0}]apad=pad_dur={1:.3f}[apd]".format(label, hold))
        label = "apd"

    if rate != 1.0:
        chains.append("[{0}]atempo={1:.4f}[atp]".format(label, rate))
        label = "atp"

    if label in ("0:a",):
        return [], "0:a"
    chains.append("[{0}]anull[a]".format(label))
    return chains, "[a]"
