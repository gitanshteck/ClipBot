"""Rasterise stream chat into overlay frames for a clip.

Pure rendering: takes messages (already sliced and offset-corrected by the caller)
and produces PNGs plus an ffconcat list. No ffmpeg, no network beyond the image
cache, no workspace knowledge - mirroring how `reelspec.py` keeps geometry
separate so the dashboard preview and the render cannot drift apart.

Two decisions worth knowing about:

**Frames are emitted on change, not on a clock.** Chat updates when a message
arrives, so a 45s clip is tens of frames rather than the ~1,350 a 30fps sequence
would need - and the concat demuxer replays them with exact per-frame durations.
On this channel 47% of 45-second windows contain no messages at all, so most
clips are a handful of frames or none.

**Text is drawn with Pillow, which does no complex-script shaping.** Measured
against 372 real messages from this channel: 93% pure ASCII, 7% ASCII plus emoji,
zero Devanagari and zero RTL - the Hindi on this stream is spoken, and typed
chat is Hinglish in Latin script. Pillow is therefore correct here *and* renders
colour emoji, which libass cannot. `unshaped_scripts()` flags the day that stops
being true rather than letting it fail silently.
"""

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .utils import StageError, get_logger

log = get_logger(__name__)

PILLOW_HINT = "Install with: pip install Pillow"

# Codepoint ranges Pillow cannot lay out correctly without libraqm, which no
# published Windows wheel ships. Latin/emoji are unaffected: they need no
# reordering, ligature formation or bidi.
_UNSHAPED_RANGES = (
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"),
    (0x0700, 0x074F, "syriac"),
    (0x0900, 0x097F, "devanagari"),
    (0x0980, 0x0DFF, "indic"),
    (0x0E00, 0x0E7F, "thai"),
    (0xFB1D, 0xFDFF, "hebrew/arabic presentation"),
)

EMOTE_URL = "https://files.kick.com/emotes/{0}/fullsize"


def unshaped_scripts(text: str) -> List[str]:
    """Scripts in `text` that Pillow will render incorrectly."""
    hits = []
    for ch in text:
        o = ord(ch)
        for lo, hi, name in _UNSHAPED_RANGES:
            if lo <= o <= hi and name not in hits:
                hits.append(name)
    return hits


# --------------------------------------------------------------------------
# style
# --------------------------------------------------------------------------


DEFAULT_STYLE = {
    "font": "C:/Windows/Fonts/segoeuib.ttf",
    "font_regular": "C:/Windows/Fonts/segoeui.ttf",
    "font_emoji": "C:/Windows/Fonts/seguiemj.ttf",
    "font_size": 30,
    "line_spacing": 8,
    "message_spacing": 14,
    "padding": 22,
    "panel_color": "#18181B",
    "panel_opacity": 0.90,
    "text_color": "#EFEFF1",
    "shadow": True,
    "max_messages": 12,
    "max_message_chars": 200,
    "max_messages_per_second": 4,
    "emote_size": 34,
    "badge_size": 22,
    "show_badges": True,
    "fade_seconds": 0.0,
}


def resolve_style(settings=None) -> Dict[str, Any]:
    """Merge the shipped defaults with `chat.*` from settings.json."""
    style = dict(DEFAULT_STYLE)
    if settings is not None:
        for key in list(style):
            value = settings.get("chat." + key, None)
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
# image cache
# --------------------------------------------------------------------------


class ImageCache(object):
    """Emote and badge artwork, cached on disk across workspaces.

    A missing or broken image degrades to a text fallback - a chat overlay must
    never fail a render because a CDN blinked.
    """

    def __init__(self, root: Path, impersonate: str = "chrome", timeout: int = 15):
        self.root = Path(root)
        self.impersonate = impersonate
        self.timeout = timeout
        self._memory: Dict[str, Any] = {}
        self._failed = set()

    def _download(self, url: str, path: Path) -> bool:
        try:
            from curl_cffi import requests
        except ImportError:
            return False
        try:
            response = requests.get(url, impersonate=self.impersonate, timeout=self.timeout)
            if response.status_code != 200 or not response.content:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(response.content)
            tmp.replace(path)
            return True
        except Exception as exc:
            log.debug("Could not fetch %s: %s", url, exc)
            return False

    def get(self, key: str, url: str, size: int):
        """An RGBA thumbnail `size` px tall, or None."""
        memo = "{0}@{1}".format(key, size)
        if memo in self._memory:
            return self._memory[memo]
        if key in self._failed:
            return None

        from PIL import Image

        path = self.root / "{0}.img".format(key.replace("/", "_"))
        if not path.exists() and not self._download(url, path):
            self._failed.add(key)
            return None
        try:
            with Image.open(path) as raw:
                # Animated emotes: take the first frame. Nothing here can drive
                # an animation, and a still emote reads fine at this size.
                raw.seek(0)
                image = raw.convert("RGBA")
            ratio = size / float(image.height or size)
            image = image.resize(
                (max(1, int(image.width * ratio)), size), Image.LANCZOS
            )
        except Exception as exc:
            log.debug("Could not decode %s: %s", path, exc)
            self._failed.add(key)
            return None
        self._memory[memo] = image
        return image


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------


class _Fonts(object):
    def __init__(self, style):
        from PIL import ImageFont

        size = int(style["font_size"])
        self.name = ImageFont.truetype(style["font"], size)
        self.body = ImageFont.truetype(style["font_regular"], size)
        try:
            # Segoe UI Emoji only has strikes at 109px; Pillow scales from there.
            self.emoji = ImageFont.truetype(style["font_emoji"], size)
        except OSError:
            self.emoji = None
        self.line_height = size + int(style["line_spacing"])


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (
        o >= 0x1F000
        or 0x2600 <= o <= 0x27BF
        or o in (0xFE0F, 0x200D, 0x20E3)
        or 0x1F1E6 <= o <= 0x1F1FF
    )


def _runs(text: str):
    """Split into (is_emoji, chunk) runs so emoji can use the colour font.

    Grapheme-aware to the extent that matters here: ZWJ sequences, variation
    selectors and regional-indicator pairs all classify as emoji, so a compound
    emoji stays in one run instead of being torn apart mid-sequence.
    """
    out = []
    for ch in text:
        flag = _is_emoji(ch)
        if out and out[-1][0] == flag:
            out[-1][1].append(ch)
        else:
            out.append([flag, [ch]])
    return [(flag, "".join(chars)) for flag, chars in out]


class _Token(object):
    """One drawable unit on a line: a text run, an emote or a badge image."""

    __slots__ = ("kind", "text", "image", "width", "font", "color")

    def __init__(self, kind, width, text="", image=None, font=None, color=None):
        self.kind = kind
        self.width = width
        self.text = text
        self.image = image
        self.font = font
        self.color = color


def _measure(draw, text, font):
    if not text:
        return 0
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return int(font.getlength(text))


def _tokenise(draw, message, fonts, style, cache, text_color):
    """Flatten one chat message into drawable tokens."""
    tokens: List[_Token] = []
    user = message.get("user") or {}

    if style.get("show_badges") and cache is not None:
        for badge in (user.get("badges") or [])[:4]:
            url = badge.get("image")
            if not url:
                continue
            image = cache.get("badge/" + str(badge.get("type")), url, int(style["badge_size"]))
            if image is not None:
                tokens.append(_Token("image", image.width, image=image))

    # Trailing space belongs to the name token: Kick's `content` never starts
    # with one, so without it every message reads "someuser:hello".
    name = "{0}: ".format(user.get("name") or "?")
    name_color = _rgb(user.get("color"), (145, 200, 255))
    tokens.append(
        _Token("text", _measure(draw, name, fonts.name),
               text=name, font=fonts.name, color=name_color)
    )

    limit = int(style["max_message_chars"])
    used = 0
    for part in message.get("parts") or []:
        if part.get("t") == "emote":
            image = None
            if cache is not None:
                image = cache.get(
                    "emote/" + str(part.get("id")),
                    EMOTE_URL.format(part.get("id")),
                    int(style["emote_size"]),
                )
            if image is not None:
                tokens.append(_Token("image", image.width, image=image))
                continue
            # Fall back to the emote's name so the message still reads.
            label = ":{0}:".format(part.get("name") or "emote")
            tokens.append(
                _Token("text", _measure(draw, label, fonts.body),
                       text=label, font=fonts.body, color=(160, 165, 175))
            )
            continue

        text = str(part.get("v") or "")
        if used + len(text) > limit:
            text = text[: max(0, limit - used)] + "\u2026"
        used += len(text)
        for is_emoji, chunk in _runs(text):
            font = fonts.emoji if (is_emoji and fonts.emoji) else fonts.body
            for word in _split_words(chunk):
                tokens.append(
                    _Token("text", _measure(draw, word, font),
                           text=word, font=font,
                           color=None if is_emoji else text_color)
                )
        if used >= limit:
            break
    return tokens


def _split_words(text: str) -> List[str]:
    """Keep trailing spaces attached so wrapping doesn't lose word gaps."""
    out: List[str] = []
    current = ""
    for ch in text:
        current += ch
        if ch == " ":
            out.append(current)
            current = ""
    if current:
        out.append(current)
    return out


def _wrap(tokens, max_width, space_width):
    """Greedy wrap into lines of tokens. Returns [[token, ...], ...]."""
    lines: List[List[_Token]] = [[]]
    width = 0
    for token in tokens:
        gap = 0
        if lines[-1] and not (token.kind == "text" and token.text.startswith(" ")):
            gap = space_width if token.kind == "image" or lines[-1][-1].kind == "image" else 0
        if lines[-1] and width + gap + token.width > max_width:
            lines.append([])
            width = 0
            gap = 0
        token.width += gap
        lines[-1].append(token)
        width += token.width
    return [line for line in lines if line]


def layout_message(draw, message, fonts, style, cache, max_width, text_color):
    tokens = _tokenise(draw, message, fonts, style, cache, text_color)
    space = _measure(draw, " ", fonts.body) or 6
    return _wrap(tokens, max_width, space)


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------


def _visible_at(messages, now, style):
    """Messages on screen at `now`, newest last, capped by max_messages."""
    shown = [m for m in messages if m["offset"] <= now]
    return shown[-int(style["max_messages"]):]


def _throttle(messages, style):
    """Drop messages that arrive faster than the panel can be read.

    A hype spike scrolls chat past faster than anyone can follow, and in a clip
    it just looks like flicker. Keeps the first N in any one-second bucket.
    """
    cap = int(style.get("max_messages_per_second") or 0)
    if cap <= 0:
        return messages
    kept: List[Dict[str, Any]] = []
    bucket = None
    count = 0
    for message in messages:
        second = int(message["offset"])
        if second != bucket:
            bucket, count = second, 0
        if count < cap:
            kept.append(message)
            count += 1
    return kept


def render_frames(
    messages: Sequence[Dict[str, Any]],
    width: int,
    height: int,
    duration: float,
    out_dir: Path,
    style: Dict[str, Any],
    cache: Optional[ImageCache] = None,
    opaque: bool = False,
) -> Optional[Path]:
    """Render the chat panel across a clip.

    `messages` carry offsets relative to the clip's own start (0..duration).
    Returns the ffconcat list path, or None when there is nothing to draw.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise StageError("Pillow is required to render chat overlays.\n" + PILLOW_HINT)

    messages = _throttle(sorted(messages, key=lambda m: m["offset"]), style)
    if not messages:
        return None

    flagged = set()
    for message in messages:
        for script in unshaped_scripts(message.get("text") or ""):
            flagged.add(script)
    if flagged:
        log.warning(
            "Chat contains %s, which Pillow cannot shape correctly (no libraqm "
            "in any published wheel). Those messages will render with unjoined, "
            "unreordered glyphs.",
            ", ".join(sorted(flagged)),
        )

    fonts = _Fonts(style)
    padding = int(style["padding"])
    max_width = width - padding * 2
    text_color = _rgb(style["text_color"], (239, 239, 241))

    panel_rgb = _rgb(style["panel_color"], (24, 24, 27))
    panel_alpha = int(round(float(style["panel_opacity"]) * 255)) if not opaque else 255
    background = panel_rgb + (panel_alpha if not opaque else 255,)

    # Lay every message out once. Layout is independent of time, so doing it per
    # frame would re-measure the same text dozens of times.
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    laid: Dict[str, List[List[_Token]]] = {}
    for message in messages:
        laid[message["id"]] = layout_message(
            probe, message, fonts, style, cache, max_width, text_color
        )

    # A frame is needed whenever the visible set changes: at t=0 and at each
    # message arrival inside the clip.
    times = [0.0] + [m["offset"] for m in messages if 0.0 < m["offset"] < duration]
    times = sorted(set(round(t, 3) for t in times))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("f_*.png"):
        stale.unlink()

    frames: List[Tuple[str, float]] = []
    for index, start in enumerate(times):
        end = times[index + 1] if index + 1 < len(times) else duration
        if end - start <= 0.001:
            continue

        image = Image.new("RGBA", (width, height), background)
        draw = ImageDraw.Draw(image)

        visible = _visible_at(messages, start, style)
        blocks = [laid[m["id"]] for m in visible]

        # Bottom-anchored, like a real chat panel: the newest message sits at
        # the bottom edge and older ones ride up out of the panel.
        total = sum(
            len(lines) * fonts.line_height + int(style["message_spacing"])
            for lines in blocks
        )
        y = height - padding - total if total < height - padding * 2 else padding

        for lines in blocks:
            for line in lines:
                x = padding
                for token in line:
                    if token.kind == "image" and token.image is not None:
                        image.alpha_composite(
                            token.image,
                            (int(x + token.width - token.image.width),
                             int(y + (fonts.line_height - token.image.height) / 2)),
                        )
                    elif token.text:
                        tx = x + (token.width - _measure(draw, token.text, token.font))
                        # Emoji (color is None) are drawn from the font's own
                        # colour layers; a black pass underneath just haloes them.
                        if style.get("shadow") and token.color is not None:
                            draw.text((tx + 2, y + 2), token.text, font=token.font,
                                      fill=(0, 0, 0, 190), embedded_color=False)
                        draw.text(
                            (tx, y), token.text, font=token.font,
                            fill=token.color, embedded_color=token.color is None,
                        )
                    x += token.width
                y += fonts.line_height
            y += int(style["message_spacing"])

        name = "f_{0:05d}.png".format(len(frames))
        image.save(out_dir / name, optimize=False, compress_level=1)
        frames.append((name, end - start))

    if not frames:
        return None

    list_path = out_dir / "list.txt"
    lines = ["ffconcat version 1.0"]
    for name, seconds in frames:
        lines.append("file {0}".format(name))
        lines.append("duration {0:.3f}".format(seconds))
    # The concat demuxer ignores the final entry's duration unless the file is
    # named once more; without this the last frame shows for a single frame.
    lines.append("file {0}".format(frames[-1][0]))
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    log.debug("Rendered %d chat frame(s) into %s", len(frames), out_dir)
    return list_path
