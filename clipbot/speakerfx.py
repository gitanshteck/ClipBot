"""Speaker avatar/ring overlays: resolve speaking spans into a render-ready
plan, and the ffmpeg filter fragments for it.

Not a "pure math, no IO" module like `reelspec.py`/`fxspec.py` - unlike
those, this one has to actually produce two kinds of derived images that
don't exist anywhere on disk until asked for: a circle-cropped version of
each speaker's avatar, and the coloured ring drawn around it while they're
talking. Both are cached by content in `<work_root>/_cache/avatars/`, the
same shared-across-workspaces location `chatrender.ImageCache` already uses
for emote/badge artwork, since neither depends on anything clip-specific.

Structurally, `resolve_speakers` is the avatar-overlay analogue of
`fxspec.resolve_fx`, and `build_speaker_chains` is the analogue of
`fxspec.build_overlay_chains`'s sticker handling - each speaker
appearance is a still image (`-loop 1 -i ...`), scaled to its slot and
toggled on/off via `enable=between(start,end)`, exactly like a sticker.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .reelspec import layout_speaker_slots
from .specerror import SpecError

AVATAR_FADE_SECONDS = 0.15


def _rgb(value: str, default=(25, 162, 210)) -> Tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return default
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return default


def _ensure_circular_avatar(cache_dir: Path, source_path: str, size: int) -> Path:
    """Cover-crop `source_path` to a square, resize to `size`, and mask it to
    a circle. Cached by (source path, size) - a stream's co-host avatar
    doesn't change between clips or between workspaces."""
    key = hashlib.sha1("{0}|{1}".format(source_path, size).encode("utf-8")).hexdigest()[:16]
    out = cache_dir / "avatar_{0}.png".format(key)
    if out.exists():
        return out

    from PIL import Image, ImageDraw

    with Image.open(source_path) as raw:
        img = raw.convert("RGBA")
        w0, h0 = img.size
        side = min(w0, h0)
        left, top = (w0 - side) // 2, (h0 - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize(
            (size, size), Image.LANCZOS
        )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        img.putalpha(mask)
        cache_dir.mkdir(parents=True, exist_ok=True)
        img.save(out)
    return out


def _ensure_ring(cache_dir: Path, size: int, width_px: int, color: str) -> Path:
    """A transparent PNG with just a coloured circle outline - the "lights
    up" ring, overlaid on top of an always-visible avatar in discord mode."""
    key = hashlib.sha1(
        "{0}|{1}|{2}".format(size, width_px, color).encode("utf-8")
    ).hexdigest()[:16]
    out = cache_dir / "ring_{0}.png".format(key)
    if out.exists():
        return out

    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inset = max(1, width_px // 2) + 1
    ImageDraw.Draw(img).ellipse(
        [inset, inset, size - inset, size - inset],
        outline=_rgb(color) + (255,), width=max(1, width_px),
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def resolve_speakers(
    speakers_plan: Optional[Dict[str, Any]],
    spans: List[Dict[str, Any]],
    canvas: Tuple[int, int],
    origin: float,
    span_duration: float,
    assets: Optional[Dict[str, Dict[str, Any]]] = None,
    registry: Optional[Dict[str, Dict[str, Any]]] = None,
    cache_dir: Optional[Path] = None,
    next_input: int = 1,
    strict: bool = True,
) -> Optional[Dict[str, Any]]:
    """Turn absolute-VOD-time speaking spans into a render-ready overlay plan.

    `spans` is `clipbot.speakers.speaking_spans()`'s output for the whole
    workspace; `origin`/`span_duration` rebase them onto this clip's own
    0..span_duration timeline, the same convention `fxspec.resolve_fx` uses.
    `assets` (library asset id -> {path, ...}) and `registry` (library
    speaker id -> {name, avatar_asset, color, ...}) are injected by the
    caller, same pattern `fxspec.resolve_fx` uses for its own assets.

    `strict` controls whether a speaker with no avatar configured fails the
    render (`SpecError`, real render) or is skipped with a warning (the
    dashboard's non-rendering plan preview) - mirrors `fxspec.resolve_fx`.
    Returns None when nothing survives, signalling the frozen/no-speakers
    filter path.
    """
    if not speakers_plan:
        return None
    assets = assets or {}
    registry = registry or {}
    cw, ch = int(canvas[0]), int(canvas[1])
    mode = speakers_plan["mode"]

    local_spans = []
    for sp in spans:
        start = round(float(sp["start"]) - origin, 3)
        end = round(float(sp["end"]) - origin, 3)
        if end <= 0 or start >= span_duration:
            continue
        local_spans.append({
            "speaker_id": sp["speaker_id"],
            "start": max(0.0, start),
            "end": min(span_duration, end),
        })
    if not local_spans:
        return None

    active_ids: List[str] = []
    for sp in local_spans:
        if sp["speaker_id"] not in active_ids:
            active_ids.append(sp["speaker_id"])

    slots = {s["speaker_id"]: s for s in layout_speaker_slots(speakers_plan, canvas, active_ids)}
    if not slots:
        return None

    warnings: List[str] = []
    video: List[Dict[str, Any]] = []
    inputs: List[Dict[str, Any]] = []
    index = int(next_input)

    def avatar_path(speaker_id, size):
        profile = registry.get(speaker_id)
        if not profile or not profile.get("avatar_asset"):
            return None
        asset = assets.get(profile["avatar_asset"])
        if asset is None:
            return None
        if cache_dir is None:
            return asset["path"]
        return str(_ensure_circular_avatar(cache_dir, asset["path"], size))

    def add_image(kind, speaker_id, path, slot, start, end, fade=0.0):
        nonlocal index
        entry_index = index
        video.append({
            "type": kind, "speaker_id": speaker_id, "index": entry_index,
            "start": start, "end": end, "fade": fade,
            "x": slot["x"], "y": slot["y"], "w": slot["w"], "h": slot["h"],
        })
        inputs.append({
            "role": kind, "index": entry_index, "speaker_id": speaker_id,
            "args": ["-loop", "1", "-i", str(path)],
        })
        index += 1

    if mode == "discord":
        for speaker_id in active_ids:
            slot = slots.get(speaker_id)
            if slot is None:
                continue
            path = avatar_path(speaker_id, slot["w"])
            if path is None:
                message = "speakers: no avatar image set for {0!r}".format(speaker_id)
                if strict:
                    raise SpecError(message)
                warnings.append(message)
                continue
            # Always visible for the clip's whole duration - that's the point
            # of discord mode. No fade: it's already on screen from frame one.
            add_image("avatar", speaker_id, path, slot, 0.0, span_duration)

        for sp in local_spans:
            slot = slots.get(sp["speaker_id"])
            if slot is None:
                continue
            color = (registry.get(sp["speaker_id"]) or {}).get("color") or "#19A2D2"
            if cache_dir is None:
                message = "speakers: no cache directory to render the ring into"
                if strict:
                    raise SpecError(message)
                warnings.append(message)
                continue
            ring_path = _ensure_ring(cache_dir, slot["w"], speakers_plan["ring_width_px"], color)
            add_image("ring", sp["speaker_id"], ring_path, slot, sp["start"], sp["end"])
    else:  # appear
        for sp in local_spans:
            slot = slots.get(sp["speaker_id"])
            if slot is None:
                continue
            path = avatar_path(sp["speaker_id"], slot["w"])
            if path is None:
                message = "speakers: no avatar image set for {0!r}".format(sp["speaker_id"])
                if strict:
                    raise SpecError(message)
                warnings.append(message)
                continue
            add_image("avatar", sp["speaker_id"], path, slot, sp["start"], sp["end"],
                      fade=AVATAR_FADE_SECONDS)

    if not video:
        return None
    return {"video": video, "inputs": inputs, "warnings": warnings}


def build_speaker_chains(plan: Optional[Dict[str, Any]], label_in: str, canvas) -> Tuple[List[str], str]:
    """Same idiom as `fxspec.build_overlay_chains`'s sticker handling: each
    entry is a still image scaled to its slot and toggled via
    `enable=between(...)`. A discord-mode avatar with (0, span_duration)
    simply reads as always-on."""
    chains: List[str] = []
    label = label_in
    if not plan:
        return chains, label

    for entry in plan["video"]:
        tag = "spk{0}".format(entry["index"])
        parts = ["[{0}:v]scale={1}:{2}:flags=lanczos,format=rgba".format(
            entry["index"], entry["w"], entry["h"])]
        fade = entry.get("fade") or 0.0
        start, end = entry["start"], entry["end"]
        if fade > 0 and (end - start) > 2 * fade:
            parts.append("setpts=PTS-STARTPTS+{0:.3f}/TB".format(start))
            parts.append("fade=t=in:st={0:.3f}:d={1:.3f}:alpha=1".format(start, fade))
            parts.append("fade=t=out:st={0:.3f}:d={1:.3f}:alpha=1".format(end - fade, fade))
        chains.append(",".join(parts) + "[{0}]".format(tag))
        chains.append(
            "[{0}][{1}]overlay=x={2}:y={3}:enable='between(t,{4:.3f},{5:.3f})'"
            ":format=auto[{1}o]".format(label, tag, entry["x"], entry["y"], start, end)
        )
        label = tag + "o"

    return chains, label
