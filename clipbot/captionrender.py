"""Rasterise Hinglish captions into overlay frames for a clip.

Mirror of `chatrender.py`, deliberately smaller: one wrapped text block per
segment instead of per-message name/badge/emote tokens, and no image cache -
captions are pure text, so there is nothing to fetch. Pure rendering, same as
`chatrender.py`: `segments` arrive already sliced to the clip's own
0..duration window and offset-corrected by the caller (`stages/reel.py`).

The whole reason this can render at all with the same Pillow-without-libraqm
stack `chatrender.py` already uses is that the source text is Hinglish, i.e.
Latin script - see `clipbot/stages/transliterate.py` and
`chatrender.unshaped_scripts()`, which documents that Pillow cannot shape
Devanagari on this platform. Burning in the transcript as typed by Whisper
was never an option; this module is what makes it one.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .utils import StageError, get_logger

log = get_logger(__name__)

PILLOW_HINT = "Install with: pip install Pillow"

DEFAULT_STYLE = {
    "font": "C:/Windows/Fonts/segoeuib.ttf",
    "font_size": 46,
    "line_spacing": 8,
    "max_lines": 2,
    "text_color": "#FFFFFF",
    "border_w": 4,
    "border_color": "#000000",
    "shadow": True,
    "uppercase": False,
    "padding": 28,
}


def resolve_style(settings=None) -> Dict[str, Any]:
    """Merge the shipped defaults with `reel.captions.*` from settings.json."""
    style = dict(DEFAULT_STYLE)
    if settings is not None:
        for key in list(style):
            value = settings.get("reel.captions." + key, None)
            if value is not None:
                style[key] = value
    return style


def style_signature(style: Dict[str, Any]) -> str:
    """Stable hash of the style, so a settings tweak re-renders the frames."""
    blob = json.dumps(style, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _rgb(value: str, default=(255, 255, 255)) -> Tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return default
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return default


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------


def _measure(draw, text, font):
    if not text:
        return 0
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return int(font.getlength(text))


def _wrap(draw, text, font, max_width) -> List[str]:
    """Greedy word-wrap. No per-character run splitting - captions are plain
    Latin-script text, unlike chat which has to interleave emote/badge images."""
    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if _measure(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------


def _clean_segments(segments, duration) -> List[Dict[str, Any]]:
    """Clip each segment to [0, duration] and drop anything left with no span
    or no text. Captions are intervals (unlike chat messages, which are point
    events that persist until scrolled off) - each must vanish at its own end."""
    out = []
    for seg in segments:
        start = max(0.0, float(seg.get("start", 0.0)))
        end = min(float(duration), float(seg.get("end", 0.0)))
        text = str(seg.get("text") or "").strip()
        if not text or end - start <= 0.01:
            continue
        out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    out.sort(key=lambda s: s["start"])
    return out


def render_frames(
    segments: Sequence[Dict[str, Any]],
    width: int,
    height: int,
    duration: float,
    out_dir: Path,
    style: Dict[str, Any],
) -> Optional[Path]:
    """Render captions across a clip. Returns the ffconcat list path, or None
    when nothing in the window survives clipping (a clip with no speech, or
    whose captions.offset shifted every segment outside the window)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise StageError("Pillow is required to render caption overlays.\n" + PILLOW_HINT)

    segs = _clean_segments(segments, duration)
    if not segs:
        return None

    font = ImageFont.truetype(style["font"], int(style["font_size"]))
    text_color = _rgb(style["text_color"], (255, 255, 255))
    border_color = _rgb(style["border_color"], (0, 0, 0))
    border_w = int(style["border_w"])
    padding = int(style["padding"])
    max_width = max(1, width - padding * 2)
    max_lines = max(1, int(style["max_lines"]))
    line_h = int(style["font_size"]) + int(style["line_spacing"])
    # 8-direction faux stroke rather than a full (2*border_w+1)^2 grid - the
    # standard cheap approximation, and each extra ring costs 8 more draws.
    offsets: List[Tuple[int, int]] = []
    for r in range(1, border_w + 1):
        offsets.extend([(r, 0), (-r, 0), (0, r), (0, -r), (r, r), (r, -r), (-r, r), (-r, -r)])

    # A frame is needed at every segment boundary, exactly like chatrender's
    # frame-on-change approach - but captions are intervals, so both starts
    # and ends are event times, not just arrivals.
    times = {0.0, round(float(duration), 3)}
    for seg in segs:
        if 0.0 < seg["start"] < duration:
            times.add(seg["start"])
        if 0.0 < seg["end"] < duration:
            times.add(seg["end"])
    times = sorted(times)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("f_*.png"):
        stale.unlink()

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    frames: List[Tuple[str, float]] = []

    for index, t0 in enumerate(times[:-1]):
        t1 = times[index + 1]
        if t1 - t0 <= 0.001:
            continue

        active = None
        for seg in segs:
            if seg["start"] <= t0 < seg["end"]:
                active = seg  # later segments in the (sorted) list win a tie

        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if active is not None:
            text = active["text"].upper() if style.get("uppercase") else active["text"]
            lines = _wrap(probe, text, font, max_width)[:max_lines]
            draw = ImageDraw.Draw(image)
            total_h = len(lines) * line_h
            y = max(padding, (height - total_h) // 2)
            for line in lines:
                w = _measure(draw, line, font)
                x = max(padding, (width - w) // 2)
                if border_w:
                    for dx, dy in offsets:
                        draw.text((x + dx, y + dy), line, font=font,
                                  fill=border_color + (255,))
                elif style.get("shadow"):
                    draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 190))
                draw.text((x, y), line, font=font, fill=text_color + (255,))
                y += line_h

        name = "f_{0:05d}.png".format(len(frames))
        image.save(out_dir / name, optimize=False, compress_level=1)
        frames.append((name, t1 - t0))

    if not frames:
        return None

    list_path = out_dir / "list.txt"
    lines_out = ["ffconcat version 1.0"]
    for name, seconds in frames:
        lines_out.append("file {0}".format(name))
        lines_out.append("duration {0:.3f}".format(seconds))
    # The concat demuxer ignores the final entry's duration unless the file is
    # named once more; without this the last frame shows for a single frame.
    lines_out.append("file {0}".format(frames[-1][0]))
    list_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")

    log.debug("Rendered %d caption frame(s) into %s", len(frames), out_dir)
    return list_path
