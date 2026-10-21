"""Rasterise an edit's text, callout and image overlays to transparent PNGs.

drawtext is the wrong tool for an edit's on-screen graphics: it cannot slide and
settle or pop, it takes its font from fontconfig (which the Windows ffmpeg builds
ship without, so text silently vanishes - see `fxspec.resolve_fx`), and a plate
behind the text is a second filter. So each overlay is drawn once here with
Pillow - the same dependency `chatrender.py` and `captionrender.py` already
depend on - and `editfilters` animates the finished PNG with overlay/scale/fade
expressions.

Everything is drawn at 2x and reduced with Lanczos, which is what keeps a thin
outline from shimmering after the encoder gets hold of it.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .utils import StageError

SUPERSAMPLE = 2

# First existing file wins. Bahnschrift is the closest thing a stock Windows
# install has to a gaming-HUD face; the rest are fallbacks for other machines.
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\bahnschrift.ttf",
    r"C:\Windows\Fonts\seguibl.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

DEFAULT_ACCENT = "#FFB300"


def find_font(preferred: Optional[str] = None) -> Optional[str]:
    """A real font file path, or None when the machine has none of the candidates."""
    for cand in ([preferred] if preferred else []) + list(FONT_CANDIDATES):
        if cand and Path(cand).is_file():
            return str(cand)
    return None


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError:
        raise StageError("Pillow is required for overlays: pip install -r requirements.txt")
    from PIL import Image, ImageDraw, ImageFont
    return Image, ImageDraw, ImageFont


def _rgb(color: str) -> Tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def fingerprint(item: Dict[str, Any], canvas: Tuple[int, int], font: Optional[str], icon: Optional[str]) -> str:
    """Stable hash of everything that changes the pixels: the cache key for the PNG."""
    stamp = ""
    for path in (font, icon, item.get("image")):
        if path and Path(path).is_file():
            st = Path(path).stat()
            stamp += "{0}:{1}:{2}|".format(path, st.st_size, int(st.st_mtime))
    keep = {k: item.get(k) for k in ("kind", "text", "sub", "size", "color", "accent", "plate", "w", "image", "style")}
    blob = json.dumps([keep, list(canvas), stamp], sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def render(
    item: Dict[str, Any],
    canvas: Tuple[int, int],
    out_path: Path,
    font: Optional[str] = None,
    icon: Optional[str] = None,
) -> Tuple[int, int]:
    """Draw one overlay to `out_path` (RGBA PNG) and return its (width, height)."""
    Image, ImageDraw, ImageFont = _pil()
    cw, ch = int(canvas[0]), int(canvas[1])
    kind = item["kind"]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if kind == "image":
        path = Path(item["image"])
        if not path.is_file():
            raise StageError("overlay image not found: {0}".format(path))
        im = Image.open(path).convert("RGBA")
        w = max(2, int(round(cw * item["w"])))
        h = max(2, int(round(w * im.height / float(im.width))))
        im = im.resize((w, h), Image.LANCZOS)
        im.save(out_path)
        return w, h

    ss = SUPERSAMPLE
    size_px = max(10, int(round(ch * item["size"]))) * ss
    sub_px = max(8, int(size_px * 0.62))
    fpath = find_font(font)
    if fpath:
        f_main = ImageFont.truetype(fpath, size_px)
        f_sub = ImageFont.truetype(fpath, sub_px)
    else:  # Pillow's bitmap fallback: legible, ugly, and only ever the last resort
        f_main = f_sub = ImageFont.load_default()

    text = item["text"].upper() if kind == "callout" else item["text"]
    sub = item.get("sub")
    color = _rgb(item["color"])
    accent = _rgb(item.get("accent") or DEFAULT_ACCENT)
    plate = bool(item.get("plate"))

    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))

    def measure(txt, fnt, stroke=0):
        l, t, r, b = probe.textbbox((0, 0), txt, font=fnt, stroke_width=stroke)
        return r - l, b - t, l, t

    stroke = 0 if plate else max(2, size_px // 9)
    tw, th, tl, tt = measure(text, f_main, stroke)
    sw = sh = 0
    if sub:
        sw, sh, _, _ = measure(sub, f_sub)
    icon_im = None
    if icon and Path(icon).is_file():
        icon_im = Image.open(icon).convert("RGBA")
        scale = (size_px * 1.25) / float(icon_im.height)
        icon_im = icon_im.resize((max(2, int(icon_im.width * scale)), max(2, int(icon_im.height * scale))), Image.LANCZOS)

    pad = int(size_px * 0.45) if plate else stroke * 2 + 4
    bar = int(size_px * 0.14) if plate else 0
    gap = int(size_px * 0.18)
    icon_w = (icon_im.width + gap) if icon_im else 0
    text_w = max(tw, sw)
    text_h = th + (gap // 2 + sh if sub else 0)
    inner_h = max(text_h, icon_im.height if icon_im else 0)
    W = pad + bar + (gap if bar else 0) + icon_w + text_w + pad
    H = pad + inner_h + pad
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if plate:
        draw.rounded_rectangle((0, 0, W - 1, H - 1), radius=int(size_px * 0.28), fill=(11, 11, 15, 205))
        draw.rounded_rectangle((0, 0, bar + 2, H - 1), radius=int(size_px * 0.14), fill=accent + (255,))
    x = pad + bar + (gap if bar else 0)
    if icon_im:
        img.alpha_composite(icon_im, (x, (H - icon_im.height) // 2))
        x += icon_w
    y = pad + (inner_h - text_h) // 2
    if not plate:  # soft drop shadow under outlined text
        draw.text((x - tl + stroke // 2 + 2, y - tt + stroke // 2 + 3), text, font=f_main, fill=(0, 0, 0, 150),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
    draw.text((x - tl, y - tt), text, font=f_main, fill=color + (255,), stroke_width=stroke,
              stroke_fill=(0, 0, 0, 235) if not plate else None)
    if sub:
        draw.text((x, y + th + gap // 2), sub, font=f_sub, fill=(210, 210, 220, 255))

    w2, h2 = max(2, W // ss), max(2, H // ss)
    img = img.resize((w2, h2), Image.LANCZOS)
    img.save(out_path)
    return img.width, img.height
