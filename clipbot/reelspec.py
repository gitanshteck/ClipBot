"""Geometry for vertical (9:16) reel export.

Pure math - no IO, no ffmpeg, no web framework. This module is the single source
of truth for how a clip becomes a vertical frame, imported by both the render
stage and the dashboard's preview endpoint. That shared use is deliberate: if the
browser did its own layout maths, the preview would drift from the render, which
is the classic way this feature goes wrong.

Crop rects are stored as **fractions of the source frame**, not pixels, so a spec
authored against a 720p VOD still resolves correctly if the same VOD is later
re-downloaded at 1080p.

Composition model, uniform across every layout:

    crop -> scale(force_original_aspect_ratio=increase) -> crop -> setsar=1

The scale+crop pair guarantees the region ends up exactly the target size with no
aspect distortion, however the user's dragged rect happens to round. The game
pane is then `pad`ded onto the canvas and the webcam `overlay`ed at any position,
which is what makes arbitrary webcam placement fall out for free.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .specerror import SpecError  # noqa: F401  (re-exported; see specerror.py)

# Measured off this channel's OBS layout: the webcam sits at x=32 y=216 320x180
# in a 1280x720 frame - exactly 25% scale, snapped 32px from the left edge.
# Kept as fractions so a 1080p re-download resolves to 48,324 480x270 unchanged.
DEFAULT_CAM = {"x": 0.0250, "y": 0.3000, "w": 0.2500, "h": 0.2500}

PRESETS = {
    #  preset        layout     cam_edge  cam
    "cam_top":    ("stacked", "top",    True),
    "cam_bottom": ("stacked", "bottom", True),
    "blur_fill":  ("blur",    None,     True),
    "pip":        ("full",    None,     True),
    "game_only":  ("full",    None,     False),
}

LAYOUTS = ("stacked", "blur", "full")
CAM_EDGES = ("top", "bottom")

_CANVAS_RE = re.compile(r"^(\d{2,4})x(\d{2,4})$")

# Where a free-floating cam goes when the user hasn't placed it (fractions of the
# canvas). Top-right, clear of Instagram's right-hand action column.
DEFAULT_DEST_CAM = {"x": 0.60, "y": 0.07, "w": 0.36}

OPT_RANGES = {
    "blur_sigma": (0.0, 50.0),
    "cam_border": (0.0, 40.0),
}

# Chat overlay. `panel` reserves a band and shrinks the video into what's left;
# `overlay` floats the messages on top at full size.
#
# `overlay` is the default because this channel's chat is sparse - 47% of
# 45-second windows contain no messages at all - so a reserved band would sit
# empty through half the clips while costing a third of the frame.
CHAT_MODES = ("panel", "overlay")
# Only bottom and right are offered: with the band at the canvas origin the
# video's destination box needs no offset, which keeps `resolve` unchanged.
CHAT_SIDES = ("bottom", "right")
CHAT_SIZE_RANGE = (0.10, 0.60)

# Hinglish caption overlay (clipbot/captionrender.py). Always full-width and
# floats over the video like chat's `overlay` mode - never `panel`, i.e. it
# never reserves canvas space, because a clip's speech coverage varies far
# more than chat volume does and a permanently reserved band would waste a
# third of the frame on clips with light narration.
CAPTION_POSITIONS = ("bottom", "top", "center")
CAPTION_SIZE_RANGE = (0.05, 0.45)
CAPTION_STYLE_KEYS = (
    "font_size", "line_spacing", "text_color", "border_w", "border_color",
    "shadow", "uppercase",
)

# Speaker avatar overlay (clipbot/speakerfx.py). "appear": the avatar shows
# only while that speaker is talking. "discord": every speaker who appears
# anywhere in the clip stays docked at their slot for the whole clip, and a
# coloured ring lights up around whoever is talking at that instant - the
# voice-call-UI look the name refers to.
SPEAKER_MODES = ("appear", "discord")
SPEAKER_EDGES = ("bottom", "top")
SPEAKER_AVATAR_SIZE_RANGE = (0.05, 0.40)
SPEAKER_GAP_RANGE = (0.0, 0.10)
SPEAKER_RING_WIDTH_RANGE = (0, 20)
SPEAKER_ROSTER_MAX = 8


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _frac(value: Any, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise SpecError("{0} must be a number, got {1!r}".format(name, value))
    if not (0.0 <= f <= 1.0):
        raise SpecError("{0} must be between 0 and 1, got {1}".format(name, f))
    return round(f, 4)


def _rect(value: Any, name: str, need_h: bool = True) -> Dict[str, float]:
    if not isinstance(value, dict):
        raise SpecError("{0} must be an object with x/y/w/h".format(name))
    out = {
        "x": _frac(value.get("x", 0.0), name + ".x"),
        "y": _frac(value.get("y", 0.0), name + ".y"),
        "w": _frac(value.get("w", 1.0), name + ".w"),
    }
    if need_h:
        out["h"] = _frac(value.get("h", 1.0), name + ".h")
    if out["w"] < 0.01:
        raise SpecError("{0}.w is too small".format(name))
    if need_h and out["h"] < 0.01:
        raise SpecError("{0}.h is too small".format(name))
    if out["x"] + out["w"] > 1.0001:
        raise SpecError("{0} runs past the right edge".format(name))
    if need_h and out["y"] + out["h"] > 1.0001:
        raise SpecError("{0} runs past the bottom edge".format(name))
    return out


def parse_canvas(text: Any) -> Tuple[int, int]:
    match = _CANVAS_RE.match(str(text or ""))
    if not match:
        raise SpecError("canvas must look like 1080x1920, got {0!r}".format(text))
    w, h = int(match.group(1)), int(match.group(2))
    if w % 2 or h % 2:
        raise SpecError("canvas dimensions must be even")
    if w > 4096 or h > 4096 or w < 64 or h < 64:
        raise SpecError("canvas dimensions out of range")
    return w, h


def normalize(spec: Any) -> Dict[str, Any]:
    """Validate a user-supplied spec. Raises SpecError (a ValueError) on bad input.

    Raising rather than coercing matters: the PATCH route maps ValueError to a
    400, so the UI gets a real error instead of a silent no-op.
    """
    if not isinstance(spec, dict):
        raise SpecError("reel spec must be an object")

    preset = spec.get("preset", "cam_top")
    if preset not in PRESETS:
        raise SpecError(
            "unknown preset {0!r} - expected one of {1}".format(
                preset, ", ".join(sorted(PRESETS))
            )
        )
    layout, cam_edge, cam_enabled = PRESETS[preset]

    # Explicit overrides win over the preset's implied geometry.
    layout = spec.get("layout", layout)
    if layout not in LAYOUTS:
        raise SpecError("unknown layout {0!r}".format(layout))
    cam_edge = spec.get("cam_edge", cam_edge)
    if cam_edge is not None and cam_edge not in CAM_EDGES:
        raise SpecError("cam_edge must be 'top', 'bottom' or null")
    cam_enabled = bool(spec.get("cam_enabled", cam_enabled))

    src = spec.get("src") or {}
    out_src: Dict[str, Any] = {}
    if src.get("game") is not None:
        out_src["game"] = _rect(src["game"], "src.game")
    out_src["cam"] = _rect(src.get("cam") or DEFAULT_CAM, "src.cam")

    dest_cam = spec.get("dest_cam")
    out_dest = _rect(dest_cam, "dest_cam", need_h=False) if dest_cam else None

    canvas = spec.get("canvas", "1080x1920")
    parse_canvas(canvas)  # validate now, resolve later

    opts_in = spec.get("opts") or {}
    opts: Dict[str, Any] = {}
    for key, (lo, hi) in OPT_RANGES.items():
        if key in opts_in:
            try:
                opts[key] = max(lo, min(hi, float(opts_in[key])))
            except (TypeError, ValueError):
                raise SpecError("opts.{0} must be a number".format(key))
    for flag in ("mirror_cam", "loudnorm"):
        if flag in opts_in:
            opts[flag] = bool(opts_in[flag])

    out = {
        "preset": preset,
        "layout": layout,
        "cam_edge": cam_edge,
        "cam_enabled": cam_enabled,
        "src": out_src,
        "dest_cam": out_dest,
        "canvas": str(canvas),
        "opts": opts,
    }
    chat = _chat_block(spec.get("chat"))
    if chat is not None:
        out["chat"] = chat

    captions = _captions_block(spec.get("captions"))
    if captions is not None:
        out["captions"] = captions

    speakers = _speakers_block(spec.get("speakers"))
    if speakers is not None:
        out["speakers"] = speakers

    # Only emitted when non-empty, mirroring `chat` above: an effects-free clip
    # must round-trip to exactly the dict it had before this feature existed,
    # or re-saving one would churn clips.json for no reason.
    from . import fxspec

    fx = fxspec.normalize_fx(spec.get("fx"))
    if fx:
        out["fx"] = fx
    return out


def _chat_block(chat):
    """Validate the chat overlay block, or None when chat is off."""
    if chat is None:
        return None
    if not isinstance(chat, dict):
        raise SpecError("chat must be an object")
    if not chat.get("enabled", True):
        return None

    mode = chat.get("mode", "overlay")
    if mode not in CHAT_MODES:
        raise SpecError(
            "chat.mode must be one of {0}, got {1!r}".format(", ".join(CHAT_MODES), mode)
        )
    side = chat.get("side", "bottom")
    if side not in CHAT_SIDES:
        raise SpecError(
            "chat.side must be one of {0}, got {1!r}".format(", ".join(CHAT_SIDES), side)
        )
    lo, hi = CHAT_SIZE_RANGE
    try:
        size = float(chat.get("size", 0.34))
    except (TypeError, ValueError):
        raise SpecError("chat.size must be a number")
    if not (lo <= size <= hi):
        raise SpecError("chat.size must be between {0} and {1}, got {2}".format(lo, hi, size))

    offset = chat.get("offset")
    if offset is not None:
        try:
            offset = round(float(offset), 3)
        except (TypeError, ValueError):
            raise SpecError("chat.offset must be a number of seconds or null")

    return {
        "enabled": True,
        "mode": mode,
        "side": side,
        "size": round(size, 4),
        # None means "use the workspace's chat_offset_seconds"; a number
        # overrides it for this clip, which is what a mid-stream reconnect needs.
        "offset": offset,
    }


def _captions_block(captions):
    """Validate the captions overlay block, or None when captions are off.

    Mirrors `_chat_block` in shape. `offset` exists for the same reason as
    `chat.offset`: captions.json is authored on the VOD's own clock (no
    stream-start correction needed, unlike chat), but a per-clip nudge is
    still occasionally worth having - e.g. a clip whose padded start lands a
    beat before a segment's boundary.
    """
    if captions is None:
        return None
    if not isinstance(captions, dict):
        raise SpecError("captions must be an object")
    if not captions.get("enabled", True):
        return None

    position = captions.get("position", "bottom")
    if position not in CAPTION_POSITIONS:
        raise SpecError(
            "captions.position must be one of {0}, got {1!r}".format(
                ", ".join(CAPTION_POSITIONS), position
            )
        )
    lo, hi = CAPTION_SIZE_RANGE
    try:
        size = float(captions.get("size", 0.18))
    except (TypeError, ValueError):
        raise SpecError("captions.size must be a number")
    if not (lo <= size <= hi):
        raise SpecError(
            "captions.size must be between {0} and {1}, got {2}".format(lo, hi, size)
        )

    try:
        max_lines = int(captions.get("max_lines", 2))
    except (TypeError, ValueError):
        raise SpecError("captions.max_lines must be an integer")
    if not (1 <= max_lines <= 5):
        raise SpecError("captions.max_lines must be between 1 and 5")

    offset = captions.get("offset")
    if offset is not None:
        try:
            offset = round(float(offset), 3)
        except (TypeError, ValueError):
            raise SpecError("captions.offset must be a number of seconds or null")

    style = captions.get("style")
    out_style = None
    if style is not None:
        if not isinstance(style, dict):
            raise SpecError("captions.style must be an object")
        # Light validation only: an unrecognised colour string degrades to
        # captionrender's own default rather than failing the render, the
        # same trade fxspec's TIMED_TYPES effects don't make (those raise) -
        # a caption misconfiguration should never be able to break a reel
        # that would otherwise render fine.
        out_style = {k: style[k] for k in CAPTION_STYLE_KEYS if k in style}

    return {
        "enabled": True,
        "position": position,
        "size": round(size, 4),
        "max_lines": max_lines,
        "offset": offset,
        "style": out_style,
    }


def _speakers_block(speakers):
    """Validate the speaker-avatar overlay block, or None when it's off."""
    if speakers is None:
        return None
    if not isinstance(speakers, dict):
        raise SpecError("speakers must be an object")
    if not speakers.get("enabled", True):
        return None

    mode = speakers.get("mode", "appear")
    if mode not in SPEAKER_MODES:
        raise SpecError(
            "speakers.mode must be one of {0}, got {1!r}".format(
                ", ".join(SPEAKER_MODES), mode
            )
        )
    edge = speakers.get("edge", "bottom")
    if edge not in SPEAKER_EDGES:
        raise SpecError(
            "speakers.edge must be one of {0}, got {1!r}".format(
                ", ".join(SPEAKER_EDGES), edge
            )
        )

    lo, hi = SPEAKER_AVATAR_SIZE_RANGE
    try:
        avatar_size = float(speakers.get("avatar_size", 0.16))
    except (TypeError, ValueError):
        raise SpecError("speakers.avatar_size must be a number")
    if not (lo <= avatar_size <= hi):
        raise SpecError(
            "speakers.avatar_size must be between {0} and {1}, got {2}".format(
                lo, hi, avatar_size
            )
        )

    lo, hi = SPEAKER_GAP_RANGE
    try:
        gap = float(speakers.get("gap", 0.02))
    except (TypeError, ValueError):
        raise SpecError("speakers.gap must be a number")
    if not (lo <= gap <= hi):
        raise SpecError(
            "speakers.gap must be between {0} and {1}, got {2}".format(lo, hi, gap)
        )

    lo, hi = SPEAKER_RING_WIDTH_RANGE
    try:
        ring_width_px = int(speakers.get("ring_width_px", 6))
    except (TypeError, ValueError):
        raise SpecError("speakers.ring_width_px must be an integer")
    if not (lo <= ring_width_px <= hi):
        raise SpecError(
            "speakers.ring_width_px must be between {0} and {1}".format(lo, hi)
        )

    roster = speakers.get("roster")
    out_roster = None
    if roster is not None:
        if not isinstance(roster, list) or not all(isinstance(r, str) and r for r in roster):
            raise SpecError("speakers.roster must be a list of speaker ids")
        if len(roster) > SPEAKER_ROSTER_MAX:
            raise SpecError(
                "speakers.roster is too long (limit {0})".format(SPEAKER_ROSTER_MAX)
            )
        out_roster = [str(r) for r in roster]

    return {
        "enabled": True,
        "mode": mode,
        "edge": edge,
        "avatar_size": round(avatar_size, 4),
        "gap": round(gap, 4),
        "ring_width_px": ring_width_px,
        "roster": out_roster,
    }


def layout_speaker_slots(
    speakers_plan: Dict[str, Any], canvas, active_ids: List[str]
) -> List[Dict[str, Any]]:
    """Lay out avatar slots for the speakers actually present in a clip.

    Pure math, no IO: `active_ids` (which speakers appear, in the order they
    should be laid out) is derived by the caller from
    `clipbot.speakers.speaking_spans()` - this module never touches a
    workspace. An explicit `roster` on the spec wins over the caller's
    order/subset entirely (a fixed, on-brand layout regardless of who
    actually talks in a given clip); without one, slots follow `active_ids`
    in the order given (first appearance), evenly spaced along `edge`.
    """
    if not speakers_plan:
        return []
    cw, ch = int(canvas[0]), int(canvas[1])
    roster = speakers_plan.get("roster")
    ids = list(roster) if roster else list(dict.fromkeys(active_ids))
    if not ids:
        return []

    d = max(4, _even(cw * speakers_plan["avatar_size"]))
    gap_px = _even(cw * speakers_plan["gap"])
    margin = _even(ch * 0.04)
    total = len(ids) * d + max(0, len(ids) - 1) * gap_px
    start_x = max(0, _even((cw - total) / 2.0))
    y = margin if speakers_plan["edge"] == "top" else ch - d - margin

    slots = []
    x = start_x
    for speaker_id in ids:
        slots.append({"speaker_id": speaker_id, "x": x, "y": y, "w": d, "h": d})
        x += d + gap_px
    return slots


# --------------------------------------------------------------------------
# resolution: fractions -> pixels
# --------------------------------------------------------------------------


def _even(n: float) -> int:
    """Round to an even integer. yuv420p subsamples chroma 2x2, so odd
    dimensions are either rejected or silently fudged by ffmpeg."""
    i = int(round(n))
    return i - (i % 2)


def _clamp_rect(x, y, w, h, max_w, max_h):
    w = max(2, min(_even(w), _even(max_w)))
    h = max(2, min(_even(h), _even(max_h)))
    x = max(0, min(_even(x), _even(max_w) - w))
    y = max(0, min(_even(y), _even(max_h) - h))
    return x, y, w, h


def _rect_px(rect: Dict[str, float], src_w: int, src_h: int):
    return _clamp_rect(
        rect["x"] * src_w, rect["y"] * src_h,
        rect["w"] * src_w, rect.get("h", 1.0) * src_h,
        src_w, src_h,
    )


def resolve(
    spec: Dict[str, Any],
    src_w: int,
    src_h: int,
    settings: Optional[Any] = None,
) -> Dict[str, Any]:
    """Turn a validated spec plus a source size into concrete pixel rectangles.

    Returns a plan dict carrying `canvas`, `game`, `cam` (or None), `mode`,
    `opts` and `upscale` factors. Every value is already even and inside the
    frame, so both the ffmpeg builder and the browser preview can use it as-is.
    """
    spec = normalize(spec)
    full_w, full_h = parse_canvas(spec["canvas"])
    mode = spec["layout"]

    # A `panel` chat band is carved off the canvas before any geometry runs, so
    # every existing calculation below composes into the remaining box without
    # knowing chat exists. The band sits at the far edge, which is why only
    # bottom/right are allowed - the video's origin stays (0, 0).
    chat_spec = spec.get("chat")
    cw, ch = full_w, full_h
    chat_rect = None
    if chat_spec:
        if chat_spec["mode"] == "panel":
            if chat_spec["side"] == "bottom":
                band = _even(full_h * chat_spec["size"])
                ch = full_h - band
                chat_rect = [0, ch, full_w, band]
            else:
                band = _even(full_w * chat_spec["size"])
                cw = full_w - band
                chat_rect = [cw, 0, band, full_h]
        elif chat_spec["side"] == "bottom":
            chat_rect = [0, full_h - _even(full_h * chat_spec["size"]),
                         full_w, _even(full_h * chat_spec["size"])]
        else:
            chat_rect = [full_w - _even(full_w * chat_spec["size"]), 0,
                         _even(full_w * chat_spec["size"]), full_h]

    def opt(key, default):
        if key in spec["opts"]:
            return spec["opts"][key]
        if settings is not None:
            return settings.get("reel." + key, default)
        return default

    cam_src = None
    if spec["cam_enabled"]:
        cam_src = _rect_px(spec["src"]["cam"], src_w, src_h)

    # --- work out the game pane's destination box on the canvas -----------
    if mode == "stacked" and cam_src:
        # The cam band spans the full canvas width; its height follows the cam's
        # own aspect so the face is never stretched or cropped.
        cam_dst_h = _even(cw * (cam_src[3] / float(cam_src[2])))
        cam_dst_h = max(2, min(cam_dst_h, ch - 2))
        game_dst_h = ch - cam_dst_h
        if spec["cam_edge"] == "bottom":
            game_dst = (0, 0, cw, game_dst_h)
            cam_dst = (0, game_dst_h, cw, cam_dst_h)
        else:
            game_dst = (0, cam_dst_h, cw, game_dst_h)
            cam_dst = (0, 0, cw, cam_dst_h)
    elif mode == "blur":
        # Gameplay is letterboxed at native aspect over a blurred zoomed copy -
        # the one layout that never upscales the gameplay.
        fg_h = _even(cw * (src_h / float(src_w)))
        game_dst = (0, _even((ch - fg_h) / 2.0), cw, fg_h)
        cam_dst = None
    else:  # full
        game_dst = (0, 0, cw, ch)
        cam_dst = None

    # --- the game source crop --------------------------------------------
    if spec["src"].get("game"):
        game_src = _rect_px(spec["src"]["game"], src_w, src_h)
    elif mode == "blur":
        game_src = (0, 0, _even(src_w), _even(src_h))  # whole frame
    else:
        game_src = _auto_game_crop(
            game_dst, src_w, src_h, cam_src,
            avoid_cam=bool(settings.get("reel.defaults.avoid_cam", True))
            if settings is not None else True,
        )

    # --- the cam's destination, when it floats ---------------------------
    if cam_src and cam_dst is None:
        d = spec["dest_cam"] or DEFAULT_DEST_CAM
        dw = _even(d["w"] * cw)
        dh = _even(dw * (cam_src[3] / float(cam_src[2])))
        dx = max(0, min(_even(d["x"] * cw), cw - dw))
        dy = max(0, min(_even(d["y"] * ch), ch - dh))
        cam_dst = (dx, dy, dw, dh)

    plan: Dict[str, Any] = {
        # `canvas` stays the full output size so the dashboard preview and the
        # encoder agree on the frame; `content` is the box the video composes
        # into, which is smaller only when a chat panel is reserved.
        "canvas": [full_w, full_h],
        "content": [cw, ch],
        "mode": mode,
        "preset": spec["preset"],
        "game": {"src": list(game_src), "dst": list(game_dst)},
        "cam": None,
        "opts": {
            "blur_sigma": float(opt("blur_sigma", 9)),
            "cam_border": float(opt("cam_border", 4)),
            "cam_border_color": str(
                settings.get("reel.cam_border_color", "0x19A2D2")
                if settings is not None else "0x19A2D2"
            ),
            "mirror_cam": bool(spec["opts"].get("mirror_cam", False)),
            "loudnorm": bool(
                spec["opts"].get(
                    "loudnorm",
                    settings.get("reel.loudnorm", False) if settings is not None else False,
                )
            ),
            "scale_flags": str(
                settings.get("reel.scale_flags", "lanczos")
                if settings is not None else "lanczos"
            ),
            # Fill behind a reserved chat band. Matches the renderer's own panel
            # colour so the seam is invisible when chat is sparse.
            "chat_panel_color": str(
                settings.get("reel.chat_panel_color", "0x18181B")
                if settings is not None else "0x18181B"
            ),
        },
    }
    if cam_src and cam_dst:
        plan["cam"] = {"src": list(cam_src), "dst": list(cam_dst)}

    if chat_spec and chat_rect:
        plan["chat"] = {
            "rect": chat_rect,
            "mode": chat_spec["mode"],
            "side": chat_spec["side"],
            "offset": chat_spec["offset"],
        }

    captions_spec = spec.get("captions")
    if captions_spec:
        # Always full-width, floating at the full canvas size - never a
        # reserved band, unlike a chat `panel`. Positioned against
        # (full_w, full_h) rather than `content` so it sits above a reserved
        # chat panel too, same as chat's own overlay layer does.
        band = _even(full_h * captions_spec["size"])
        if captions_spec["position"] == "top":
            cap_rect = [0, 0, full_w, band]
        elif captions_spec["position"] == "center":
            cap_rect = [0, _even((full_h - band) / 2.0), full_w, band]
        else:  # bottom
            cap_rect = [0, full_h - band, full_w, band]
        plan["captions"] = {
            "rect": cap_rect,
            "max_lines": captions_spec["max_lines"],
            "offset": captions_spec["offset"],
            "style": captions_spec.get("style"),
        }

    # Carried through unresolved: turning an effect into filter arguments needs
    # the clip's time origin and the library's asset metadata, neither of which
    # this module is allowed to know about. fxspec.resolve_fx does that step.
    if spec.get("fx"):
        plan["fx"] = spec["fx"]

    # Same reasoning: which speakers actually appear in this clip depends on
    # transcript.json + speaker_map.json, which this module never touches.
    # speakerfx.resolve_speakers does that step, using layout_speaker_slots
    # (above) for the pure geometry part.
    if spec.get("speakers"):
        plan["speakers"] = spec["speakers"]

    plan["upscale"] = {
        "game": round(game_dst[2] / float(game_src[2]), 2),
        "cam": round(cam_dst[2] / float(cam_src[2]), 2) if cam_src and cam_dst else None,
    }
    return plan


def _auto_game_crop(game_dst, src_w, src_h, cam_src, avoid_cam=True):
    """Pick a gameplay crop with no user input.

    Takes the full source height, derives the width from the destination pane's
    aspect, centres it, then nudges sideways just far enough to clear the webcam
    overlay - otherwise the gameplay pane shows a sliver of the cam.
    """
    aspect = game_dst[2] / float(game_dst[3])
    # Height first, then width from it. Deriving height back out of a rounded
    # width silently loses a couple of rows.
    gh = _even(src_h)
    gw = _even(min(src_w, gh * aspect))
    if gw >= _even(src_w):  # source is narrower than the target aspect
        gw = _even(src_w)
        gh = _even(min(src_h, gw / aspect))
    gx = _even((src_w - gw) / 2.0)
    gy = _even((src_h - gh) / 2.0)

    if avoid_cam and cam_src:
        cx, cy, cwid, chgt = cam_src
        overlaps = gx < cx + cwid and cx < gx + gw
        if overlaps:
            limit = 0.15 * gw  # don't drag the action off-centre chasing the cam
            right_shift = (cx + cwid) - gx          # move right, past the cam
            left_shift = gx - (cx - gw)             # move left, before the cam
            options = []
            if right_shift <= limit and gx + right_shift + gw <= src_w:
                options.append(right_shift)
            if left_shift <= limit and gx - left_shift >= 0:
                options.append(-left_shift)
            if options:
                gx = _even(gx + min(options, key=abs))

    return _clamp_rect(gx, gy, gw, gh, src_w, src_h)


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------


def _region(label_in, src, dst_w, dst_h, flags, extra=""):
    x, y, w, h = src
    return (
        "[{0}]crop={1}:{2}:{3}:{4},"
        "scale={5}:{6}:force_original_aspect_ratio=increase:flags={7},"
        "crop={5}:{6},setsar=1{8}"
    ).format(label_in, w, h, x, y, dst_w, dst_h, flags, extra)


def overlay_input_index(chat_list, caption_list) -> Dict[str, Optional[int]]:
    """Assign ffmpeg input indices to the optional chat/caption concat inputs,
    in the fixed order `build_argv` adds them (video=0, chat, then captions).

    `build_argv` and any caller computing where fx asset inputs should start
    (`stages/reel.py`, via `fxspec.resolve_fx(next_input=...)`) both have to
    agree on this numbering - it lives in one place rather than being
    recomputed by hand in two, which is exactly how that kind of drift bug
    happens.
    """
    index = 1
    chat_input = None
    if chat_list is not None:
        chat_input = index
        index += 1
    caption_input = None
    if caption_list is not None:
        caption_input = index
        index += 1
    return {"chat": chat_input, "captions": caption_input, "next": index}


def build_filter(
    plan: Dict[str, Any],
    chat_input: Optional[int] = None,
    fx_plan: Optional[Dict[str, Any]] = None,
    caption_input: Optional[int] = None,
    speaker_plan: Optional[Dict[str, Any]] = None,
) -> str:
    """The -filter_complex string for a resolved plan.

    `chat_input` is the ffmpeg input index carrying the rendered chat frames, or
    None when there is no chat layer for this clip (chat disabled, or the clip's
    window simply had no messages - which on this channel is about half of them).
    `caption_input` is the same idea for rendered Hinglish caption frames.

    `fx_plan` is a resolved effects plan from `fxspec.resolve_fx`, or None.
    `speaker_plan` is a resolved plan from `speakerfx.resolve_speakers`, or None.

    The two paths are kept physically separate, and `_build_filter_legacy` is
    the pre-effects body moved verbatim and never edited again. That duplication
    is the point: a clip with no effects must produce a byte-identical
    -filter_complex, hence a byte-identical argv, hence the same fingerprint in
    stages/reel.py - otherwise adding this feature would silently re-render
    every reel in every workspace. tests/test_reelspec_invariant.py pins it.

    Captions and speaker avatars get the same treatment fx got: enabling
    either routes through `_build_filter_fx` (which tolerates `fx_plan=None`
    - every fxspec/speakerfx build_*_chains helper already no-ops on a falsy
    plan) rather than touching the frozen legacy path, so a clip using
    neither feature has a provably unaffected graph.
    """
    if (
        not (fx_plan and fx_plan.get("video"))
        and caption_input is None
        and not speaker_plan
    ):
        return _build_filter_legacy(plan, chat_input)
    return _build_filter_fx(
        plan, chat_input, fx_plan, caption_input=caption_input, speaker_plan=speaker_plan
    )


def _build_filter_legacy(plan: Dict[str, Any], chat_input: Optional[int] = None) -> str:
    """The effects-free graph. Frozen - do not edit. See build_filter."""
    # Compose into `content`: identical to the canvas unless a chat panel has
    # reserved a band, in which case the video is padded up to full size after.
    cw, ch = plan.get("content") or plan["canvas"]
    flags = plan["opts"]["scale_flags"]
    game = plan["game"]
    cam = plan.get("cam")
    chains: List[str] = []

    if plan["mode"] == "blur":
        sigma = plan["opts"]["blur_sigma"]
        # Blur cheaply at low resolution, then scale back up - far better looking
        # than boxblur and a fraction of the cost of gblur at full size.
        chains.append(
            "[0:v]scale={0}:{1}:force_original_aspect_ratio=increase:flags=bilinear,"
            "crop={0}:{1},gblur=sigma={2},eq=brightness=-0.06:saturation=1.25,"
            "scale={3}:{4}:flags=bilinear,setsar=1[bg]".format(
                _even(cw / 4.0), _even(ch / 4.0), sigma, cw, ch
            )
        )
        chains.append(_region("0:v", game["src"], game["dst"][2], game["dst"][3], flags) + "[fg]")
        chains.append("[bg][fg]overlay=x={0}:y={1}[t0]".format(game["dst"][0], game["dst"][1]))
        base = "t0"
    else:
        # Skip the pad when the game pane already fills the canvas (pip /
        # game_only) - it would be a no-op filter.
        fills = (game["dst"][2], game["dst"][3]) == (cw, ch)
        pad = "" if fills else ",pad={0}:{1}:{2}:{3}:color=black".format(
            cw, ch, game["dst"][0], game["dst"][1]
        )
        chains.append(
            _region("0:v", game["src"], game["dst"][2], game["dst"][3], flags, pad) + "[base]"
        )
        base = "base"

    if cam:
        extra = ""
        if plan["opts"]["mirror_cam"]:
            extra += ",hflip"
        border = int(plan["opts"]["cam_border"])
        cam_w, cam_h = cam["dst"][2], cam["dst"][3]
        if border and plan["mode"] != "stacked":
            inner_w, inner_h = _even(cam_w - border * 2), _even(cam_h - border * 2)
            chains.append(
                _region("0:v", cam["src"], inner_w, inner_h, flags, extra)
                + ",pad={0}:{1}:{2}:{2}:color={3}[cam]".format(
                    cam_w, cam_h, border, plan["opts"]["cam_border_color"]
                )
            )
        else:
            chains.append(_region("0:v", cam["src"], cam_w, cam_h, flags, extra) + "[cam]")

        if plan.get("chat"):
            # Only split the overlay off into a named [comp] pad when there's a
            # chat tail to attach - otherwise this must stay the exact fused
            # `overlay=...,format=yuv420p[v]` chain a chat-disabled reel always
            # produced, so those renders are untouched by this feature existing.
            chains.append(
                "[{0}][cam]overlay=x={1}:y={2}:format=auto[comp]".format(
                    base, cam["dst"][0], cam["dst"][1]
                )
            )
            base = "comp"
        else:
            chains.append(
                "[{0}][cam]overlay=x={1}:y={2}:format=auto,format=yuv420p[v]".format(
                    base, cam["dst"][0], cam["dst"][1]
                )
            )
            return ";".join(chains)

    if not plan.get("chat"):
        chains.append("[{0}]format=yuv420p[v]".format(base))
        return ";".join(chains)

    chains.extend(_chat_chains(plan, base, chat_input))
    return ";".join(chains)


def _video_base(plan: Dict[str, Any]) -> Tuple[List[str], str]:
    """The game+cam composition, stopping short of chat and `format=yuv420p`.

    Structurally a parallel of the first half of `_build_filter_legacy` rather
    than a refactor of it. The legacy path is frozen so that an effects-free
    clip keeps producing the exact string it always did (including that path's
    fused `overlay=...,format=yuv420p[v]` shortcut, which cannot survive having
    effects appended to it). The cost is this duplication; the benefit is that
    no edit here can invalidate an existing render.
    """
    cw, ch = plan.get("content") or plan["canvas"]
    flags = plan["opts"]["scale_flags"]
    game = plan["game"]
    cam = plan.get("cam")
    chains: List[str] = []

    if plan["mode"] == "blur":
        sigma = plan["opts"]["blur_sigma"]
        chains.append(
            "[0:v]scale={0}:{1}:force_original_aspect_ratio=increase:flags=bilinear,"
            "crop={0}:{1},gblur=sigma={2},eq=brightness=-0.06:saturation=1.25,"
            "scale={3}:{4}:flags=bilinear,setsar=1[bg]".format(
                _even(cw / 4.0), _even(ch / 4.0), sigma, cw, ch
            )
        )
        chains.append(_region("0:v", game["src"], game["dst"][2], game["dst"][3], flags) + "[fg]")
        chains.append("[bg][fg]overlay=x={0}:y={1}[t0]".format(game["dst"][0], game["dst"][1]))
        base = "t0"
    else:
        fills = (game["dst"][2], game["dst"][3]) == (cw, ch)
        pad = "" if fills else ",pad={0}:{1}:{2}:{3}:color=black".format(
            cw, ch, game["dst"][0], game["dst"][1]
        )
        chains.append(
            _region("0:v", game["src"], game["dst"][2], game["dst"][3], flags, pad) + "[base]"
        )
        base = "base"

    if cam:
        extra = ""
        if plan["opts"]["mirror_cam"]:
            extra += ",hflip"
        border = int(plan["opts"]["cam_border"])
        cam_w, cam_h = cam["dst"][2], cam["dst"][3]
        if border and plan["mode"] != "stacked":
            inner_w, inner_h = _even(cam_w - border * 2), _even(cam_h - border * 2)
            chains.append(
                _region("0:v", cam["src"], inner_w, inner_h, flags, extra)
                + ",pad={0}:{1}:{2}:{2}:color={3}[cam]".format(
                    cam_w, cam_h, border, plan["opts"]["cam_border_color"]
                )
            )
        else:
            chains.append(_region("0:v", cam["src"], cam_w, cam_h, flags, extra) + "[cam]")
        chains.append(
            "[{0}][cam]overlay=x={1}:y={2}:format=auto[comp]".format(
                base, cam["dst"][0], cam["dst"][1]
            )
        )
        base = "comp"

    return chains, base


def _build_filter_fx(plan, chat_input, fx_plan, caption_input=None, speaker_plan=None):
    """Compose, then effects. Layer order is deliberate:

        game+cam -> camera moves -> chat -> captions -> speaker avatars
                 -> stickers -> text -> flash -> freeze hold -> format

    Camera moves (punch, shake) come first so they act on the gameplay only;
    running them after the chat layer would zoom and rattle the chat box too,
    which reads as a broken overlay rather than a camera move. Captions and
    speaker avatars sit directly above chat: all three are the stream itself
    (typed, spoken, and who's-talking respectively), not the edit. Stickers
    and text sit above all of them because they *are* the edit. Flash is
    last of the visible layers because it blows out everything including the
    text - a flash that the title punches through is not a flash. The freeze
    hold is last of all so the held frame contains every layer.
    """
    from . import fxspec
    from . import speakerfx

    cw, ch = plan.get("content") or plan["canvas"]
    full_w, full_h = plan["canvas"]

    chains, base = _video_base(plan)

    # Camera moves run at content size - i.e. before a chat panel pads the
    # video up to the full canvas - so a reserved band never gets shaken.
    cam_chains, base = fxspec.build_camera_chains(fx_plan, base, cw, ch)
    chains.extend(cam_chains)

    chat_chains, base = _chat_chains_open(plan, base, chat_input)
    chains.extend(chat_chains)

    cap_chains, base = _captions_chains_open(plan, base, caption_input)
    chains.extend(cap_chains)

    speaker_chains, base = speakerfx.build_speaker_chains(speaker_plan, base, plan["canvas"])
    chains.extend(speaker_chains)

    over_chains, base = fxspec.build_overlay_chains(fx_plan, base, full_w, full_h)
    chains.extend(over_chains)

    tail_chains, base = fxspec.build_tail_chains(fx_plan, base)
    chains.extend(tail_chains)

    chains.append("[{0}]format=yuv420p[v]".format(base))
    return ";".join(chains)


def _captions_chains_open(plan, base, caption_input):
    """Overlay rendered Hinglish caption frames, same shape as
    `_chat_chains_open`. Runs after it deliberately: `_chat_chains_open`
    already pads the composed video up to the full canvas whenever a chat
    panel reserved a band, so captions land at full-canvas coordinates
    whether or not a chat panel is also active.

    `caption_input` is None both when captions are disabled for this clip and
    when they're enabled but the clip's window had no speech to render - a
    clip that's pure gameplay silence produces no caption frames at all, same
    as a clip with no chat messages produces no chat frames.
    """
    if not plan.get("captions") or caption_input is None:
        return [], base
    x, y, _w, _h = plan["captions"]["rect"]
    chains = [
        "[{0}:v]setpts=PTS-STARTPTS,format=rgba[cap]".format(caption_input),
        "[{0}][cap]overlay=x={1}:y={2}:eof_action=repeat:repeatlast=1:shortest=0"
        ":format=auto[fxcap]".format(base, x, y),
    ]
    return chains, "fxcap"


def _chat_chains_open(plan, base, chat_input):
    """`_chat_chains` without the `format=yuv420p[v]` terminator.

    Returns (chains, label) so effects can be appended after the chat layer.
    Kept separate from `_chat_chains` because that one belongs to the frozen
    legacy path.
    """
    full_w, full_h = plan["canvas"]
    content_w, content_h = plan.get("content") or plan["canvas"]
    chat = plan.get("chat")

    chains = []
    if (content_w, content_h) != (full_w, full_h):
        chains.append(
            "[{0}]pad={1}:{2}:0:0:color={3}[stage]".format(
                base, full_w, full_h, plan["opts"].get("chat_panel_color", "0x18181B")
            )
        )
        base = "stage"

    if not chat or chat_input is None:
        return chains, base

    x, y, _w, _h = chat["rect"]
    chains.append("[{0}:v]setpts=PTS-STARTPTS,format=rgba[chat]".format(chat_input))
    chains.append(
        "[{0}][chat]overlay=x={1}:y={2}:eof_action=repeat:repeatlast=1:shortest=0"
        ":format=auto[fxchat]".format(base, x, y)
    )
    return chains, "fxchat"


def _chat_chains(plan, base, chat_input):
    """Tail of the graph: pad the composed video up to the canvas if a chat
    panel reserved a band, then lay the rendered chat frames over the top."""
    full_w, full_h = plan["canvas"]
    content_w, content_h = plan.get("content") or plan["canvas"]
    chat = plan.get("chat")

    chains = []
    if (content_w, content_h) != (full_w, full_h):
        # The band is always at the far edge, so the video pads in at (0, 0).
        chains.append(
            "[{0}]pad={1}:{2}:0:0:color={3}[stage]".format(
                base, full_w, full_h, plan["opts"].get("chat_panel_color", "0x18181B")
            )
        )
        base = "stage"

    if not chat or chat_input is None:
        chains.append("[{0}]format=yuv420p[v]".format(base))
        return chains

    x, y, _w, _h = chat["rect"]
    chains.append("[{0}:v]setpts=PTS-STARTPTS,format=rgba[chat]".format(chat_input))
    chains.append(
        # eof_action/repeatlast keep the last chat frame on screen once the
        # sequence ends; shortest=0 is what stops a chat layer that runs dry
        # from truncating the video against the audio.
        "[{0}][chat]overlay=x={1}:y={2}:eof_action=repeat:repeatlast=1:shortest=0"
        ":format=auto,format=yuv420p[v]".format(base, x, y)
    )
    return chains


def build_argv(
    binary: str,
    source: str,
    out_path: str,
    start: float,
    dur: float,
    plan: Dict[str, Any],
    settings: Optional[Any] = None,
    progress_pipe: bool = True,
    chat_list: Optional[str] = None,
    fx_plan: Optional[Dict[str, Any]] = None,
    has_source_audio: bool = True,
    caption_list: Optional[str] = None,
    speaker_plan: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Full ffmpeg command for one reel.

    `chat_list` is an ffconcat list of rendered chat frames, added as a second
    input. `caption_list` is the same idea for rendered Hinglish caption
    frames - see `overlay_input_index` for the fixed ordering the two share.
    `speaker_plan` (from `speakerfx.resolve_speakers`) carries its own avatar/
    ring image inputs already indexed by the caller to start right after
    chat/captions (`stages/reel.py` computes this the same way it computes
    `fx_plan`'s `next_input`). Note that every input must be declared before
    `-t`: `-t` is only an output option because nothing follows it, and an
    `-i` placed after it would silently turn it into an input option on that
    stream. Effect assets (stickers, sound effects, music) are extra inputs
    and obey the same rule.

    Passing assets as real `-i` inputs rather than `movie=`/`amovie=` sources is
    deliberate: argv entries reach the process untouched, so a Windows path with
    a drive colon needs no escaping at all, whereas a path inside the filtergraph
    would have to survive its escaper.

    `has_source_audio` exists because `-map 0:a:0?` tolerates a source with no
    audio stream but `[0:a]` inside a filtergraph is a hard error.
    """
    from .utils import format_timestamp

    def cfg(key, default):
        return settings.get("reel." + key, default) if settings is not None else default

    argv = [binary, "-hide_banner", "-nostats", "-loglevel", "error"]
    if progress_pipe:
        argv += ["-progress", "pipe:1"]
    argv += [
        "-y",
        # Input seek, as in cut.py. With a re-encode this is frame-accurate, so
        # reels are cut more precisely than the copy-mode clips.
        "-ss", format_timestamp(start),
        "-i", str(source),
    ]
    indices = overlay_input_index(chat_list, caption_list)
    chat_input, caption_input = indices["chat"], indices["captions"]
    # Both extra -i's must land before -t: with only one input, -t is
    # unambiguously an output option, but a second -i after it would silently
    # rebind -t as an input option limiting just that stream.
    if chat_list is not None:
        argv += ["-f", "concat", "-safe", "0", "-i", str(chat_list)]
    if caption_list is not None:
        argv += ["-f", "concat", "-safe", "0", "-i", str(caption_list)]

    if not fx_plan and caption_input is None and not speaker_plan:
        # Frozen path: byte-identical to what this function produced before
        # effects (and now captions/speakers) existed, so a clip using none
        # of those features keeps its fingerprint.
        argv += [
            "-t", "{0:.3f}".format(dur),
            "-filter_complex", build_filter(plan, chat_input=chat_input),
            # Map by type: in these VODs audio is stream 0:0 and video 0:1, so a
            # positional -map 0:0 would silently produce an audio-only reel.
            "-map", "[v]", "-map", "0:a:0?",
        ]
        if plan["opts"]["loudnorm"]:
            argv += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    else:
        from . import fxspec

        for spec_in in (speaker_plan or {}).get("inputs") or []:
            argv += list(spec_in["args"])
        for spec_in in (fx_plan or {}).get("inputs") or []:
            argv += list(spec_in["args"])
        audio_chains, audio_label = fxspec.build_audio_chains(
            fx_plan, has_source_audio, bool(plan["opts"]["loudnorm"])
        )
        graph = build_filter(
            plan, chat_input=chat_input, fx_plan=fx_plan, caption_input=caption_input,
            speaker_plan=speaker_plan,
        )
        if audio_chains:
            graph = graph + ";" + ";".join(audio_chains)
        argv += [
            # A freeze hold lengthens the output and a speed change shortens it;
            # -t is what actually decides where the encode stops, so forgetting
            # this truncates the hold away with no error at all.
            "-t", "{0:.3f}".format((fx_plan or {}).get("out_duration", dur)),
            "-filter_complex", graph,
            "-map", "[v]",
        ]
        if audio_label:
            argv += ["-map", audio_label]
        elif has_source_audio:
            argv += ["-map", "0:a:0?"]
            if plan["opts"]["loudnorm"]:
                argv += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]

    argv += [
        "-c:v", str(cfg("encoder", "libx264")),
        "-preset", str(cfg("preset_x264", "slow")),
        "-crf", str(cfg("crf", 19)),
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        # Untagged vertical uploads are a known cause of washed-out iOS playback.
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-b:a", str(cfg("audio_bitrate", "192k")), "-ac", "2", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return argv


def default_spec(settings: Optional[Any] = None) -> Dict[str, Any]:
    """The shipped spec, used when a clip has no reel settings of its own."""
    cam = DEFAULT_CAM
    canvas = "1080x1920"
    preset = "cam_top"
    if settings is not None:
        cam = settings.get("reel.defaults.cam", DEFAULT_CAM) or DEFAULT_CAM
        canvas = settings.get("reel.canvas", canvas)
        preset = settings.get("reel.preset", preset)
    return {"preset": preset, "canvas": canvas, "src": {"cam": dict(cam)}}
