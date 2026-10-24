"""Captions for the edit engine: what is said, on screen, timed to the words.

Hindi gaming commentary over loud game audio is hard to follow and the speech recogniser's own text is not fit
to show (`मॉटर` for mortar, `आई गए एक`). So the text on screen is written, not copied: each clip's words
(`editwords`: Roman Hinglish or Devanagari, with times) go to a model that returns *chunks* - short subtitle lines in the Hinglish
people type (Hindi in Roman letters, English words in English), each naming the first and last recognised word
it covers. Chunks, not words, because the model's words and the recogniser's cannot be matched one to one, but
a chunk's start and end can be taken from the recogniser's words it covers, which are good to about a tenth of a
second once corrected against the audio. Inside a chunk the highlight (the word being said) is spread over the
chunk's own span in proportion to the length of each word, or taken from the recogniser's times when the counts
agree.

`captions.json` (under `work/_edits/<name>/`) is the reviewable artefact: cues in the stream's own seconds, text
the author can edit by hand. The renderer maps them through each clip's retiming to the clip's own output clock
and burns them in with libass (`ass` filter): one .ass per clip, karaoke tags for the highlight, brand font via
`fontsdir`.

Pure functions (`parse_chunks`, `cues_from_chunks`, `to_output`, `ass_document`, `censor`) and one IO function
(`generate`, which calls the model).

The recogniser is the weak link: on the first episode's Hinglish a quarter of the model's lines were guesses
(`daring times fishy like you need`). So every chunk carries the model's own `conf`; a cue under `MIN_CONF` is kept in
`captions.json` with `off: true` and not drawn. A wrong subtitle is worse than none.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import edittimeline, editwords
from .config import Settings
from .utils import StageError, get_logger

log = get_logger(__name__)

SCHEMA = 1
MAX_CHARS = 34              # one subtitle line; two would hide too much of the picture
LINGER = 0.12               # a cue stays up this long after its last word
MIN_CUE = 0.45              # ...and at least this long, so a one-word cue can be read
FADE_IN, FADE_OUT = 70, 50  # milliseconds
MIN_CONF = 0.6              # a chunk the model is less sure of than this is written down but left off the screen
MAX_WORD = 1.1              # a word is highlighted for at most this long: the recogniser stretches words over pauses

Cue = Dict[str, Any]

PROMPT = """You write on-screen subtitles for a gaming video by a Hindi-English (Hinglish) streamer.

Below are the words a speech recogniser heard in one short clip, numbered, with the time each began. The
recogniser is imperfect: the words may be in Roman letters, in Devanagari or plain English, English words may be
spelled phonetically, and some words are wrong. Return the subtitles as JSON, nothing else:

{"chunks": [{"from": 0, "to": 5, "text": "hum pe mortar chalu ho gaya", "conf": 0.9}, ...]}

`conf` (0 to 1) is how sure you are that the text is what was actually *said*, not merely what would make sense. A
wrong subtitle is worse than none: when the recognised words are too garbled to be sure, still write your best reading
but give it a `conf` under 0.5 - the editor will leave it off the screen.

Rules:
- Every chunk is one subtitle line covering recognised words `from`..`to` (inclusive). Chunks are in order, do not
  overlap, and together cover every recognised word that is real speech.
- 2 to 7 words and at most 34 characters per chunk. Break at natural pauses and phrase boundaries; a pause of over
  half a second between two words is a chunk boundary.
- Write Hinglish the way people type it: Hindi words in Roman letters (hum, nahi, chalu, bhai), English words in
  English (mortar, helicopter, headshot, guys). Light punctuation. No Devanagari.
- Use the context to fix words the recogniser plainly got wrong, but only where the right word is clear from the
  sounds and the sentence; never add content that was not said, never translate to English, never tidy the
  speaker's meaning, never make a sentence up from a few good words. Keep strong language as spoken.
- Words that are noise, a stray single sound, or not intelligible: leave them out (a chunk with "text": "" covers
  them) rather than guess.

Words (index, start second, word):
"""


# --------------------------------------------------------------------------
# chunks -> cues (pure)
# --------------------------------------------------------------------------


def words_listing(words: Sequence[Dict[str, Any]]) -> str:
    return "\n".join("{0}\t{1:.2f}\t{2}".format(i, w["s"], w["w"]) for i, w in enumerate(words))


def parse_chunks(raw: Any, n_words: int) -> List[Dict[str, Any]]:
    """The model's reply (a JSON string, or the parsed object) as validated chunks `{from, to, text}`.

    Tolerant of a reply wrapped in prose or code fences; strict about the content: indices must be in range
    and in order, a chunk that overlaps the one before it is cut back to start after it, text is trimmed to
    `MAX_CHARS` at a word boundary. A reply that cannot be read at all is a `StageError`.
    """
    obj = raw
    if isinstance(raw, str):
        text = raw.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        try:
            obj = json.loads(text)
        except ValueError:
            i = text.find("{")
            if i < 0:
                raise StageError("the captions reply was not JSON: {0!r}".format(text[:200]))
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[i:])        # the first whole object; prose after it is ignored
            except ValueError as exc:
                raise StageError("the captions reply was not JSON ({0}): {1!r}".format(exc, text[:200]))
    if not isinstance(obj, dict) or not isinstance(obj.get("chunks"), list):
        raise StageError("the captions reply has no `chunks` list")
    out: List[Dict[str, Any]] = []
    last = -1
    for ch in obj["chunks"]:
        try:
            a, b = int(ch["from"]), int(ch["to"])
            txt = str(ch.get("text", "")).strip()
        except (KeyError, TypeError, ValueError):
            continue
        try:
            conf = min(max(float(ch.get("conf", 0.8)), 0.0), 1.0)
        except (TypeError, ValueError):
            conf = 0.8
        a, b = max(a, last + 1), min(b, n_words - 1)
        if b < a:
            continue
        last = b
        txt = re.sub(r"\s+", " ", txt)
        if len(txt) > MAX_CHARS:
            cut = txt.rfind(" ", 0, MAX_CHARS)
            txt = txt[:cut if cut > 8 else MAX_CHARS].rstrip(" ,.-")
        out.append({"from": a, "to": b, "text": txt, "conf": round(conf, 2)})
    return out


def fallback_chunks(words: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chunks with the recogniser's own text, split at pauses over 0.45 s and at 6 words: what is shown when no
    model is available. Honest (it is what was heard) and ugly (Devanagari, with its errors)."""
    chunks: List[Dict[str, Any]] = []
    i = 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and j - i < 5 and words[j + 1]["s"] - words[j].get("ve", words[j]["e"]) < 0.45 \
                and len(" ".join(w["w"] for w in words[i:j + 2])) <= MAX_CHARS:
            j += 1
        chunks.append({"from": i, "to": j, "text": " ".join(w["w"] for w in words[i:j + 1]).strip(" ,.।"), "conf": 0.5})
        i = j + 1
    return chunks


def _span(w: Dict[str, Any]) -> Tuple[float, float]:
    return float(w.get("vs", w["s"])), float(w.get("ve", w["e"]))


def cues_from_chunks(chunks: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]], clip_in: float, clip_out: float,
                     min_conf: float = MIN_CONF) -> List[Cue]:
    """Cues `{s, e, text, words: [{t, s, e}]}` in the stream's seconds, for the part of the chunks inside [clip_in, clip_out].

    A cue runs from its first covered word's start to its last word's end plus `LINGER`, at least `MIN_CUE` long,
    never into the next cue, and is cut to the clip. Words the recogniser marked silent do not set the times.
    """
    cues: List[Cue] = []
    for ch in chunks:
        text = ch["text"].strip()
        if not text:
            continue
        covered = [w for w in words[ch["from"]:ch["to"] + 1] if not w.get("silent")] or list(words[ch["from"]:ch["to"] + 1])
        if not covered:
            continue
        s = _span(covered[0])[0]
        last_s, last_e = _span(covered[-1])
        e = min(last_e, last_s + MAX_WORD) + LINGER            # the last word may be stretched over a pause: do not hold the cue for it
        if e - s < MIN_CUE:
            e = s + MIN_CUE
        shown = text.split()
        if len(shown) == len(covered):
            ts = [(_span(w)[0], _span(w)[1]) for w in covered]
            # the highlight holds until the next word starts, so it never flickers off between words
            ts = [(a, min(ts[k + 1][0] if k + 1 < len(ts) else max(b, e - LINGER), a + MAX_WORD)) for k, (a, b) in enumerate(ts)]
        else:
            weights = [len(t) + 1 for t in shown]
            tot = float(sum(weights))
            ts, t0 = [], s
            for wgt in weights:
                t1 = t0 + (e - LINGER - s) * wgt / tot
                ts.append((t0, t1))
                t0 = t1
        cue = {"s": round(s, 3), "e": round(e, 3), "text": text, "conf": ch.get("conf", 1.0),
               "words": [{"t": t, "s": round(a, 3), "e": round(b, 3)} for t, (a, b) in zip(shown, ts)]}
        if cue["conf"] < min_conf:
            cue["off"] = True                                  # written down, not drawn; flip it by hand if the text is right
        cues.append(cue)
    # no overlap: a cue ends where the next begins
    for a, b in zip(cues, cues[1:]):
        if a["e"] > b["s"] - 0.02:
            a["e"] = round(max(a["s"] + 0.2, b["s"] - 0.02), 3)
    out = []
    for c in cues:
        if c["e"] <= clip_in or c["s"] >= clip_out:
            continue
        c = dict(c, s=max(c["s"], clip_in), e=min(c["e"], clip_out))
        if c["e"] - c["s"] >= 0.25:
            out.append(c)
    return out


# strong language is masked on screen (a viewer, an advertiser and a younger audience all see it), not removed
# from the sound: that is the author's decision. The list matches `editwords`; it errs towards masking.
_MASK = re.compile(r"(?<![a-z])(fuck\w*|f\*\*k\w*|shit\w*|bitch\w*|bastard\w*|asshole\w*|chutiy\w*|chut\b|behenchod|bhenchod|madarchod|maderchod|"
                   r"bhosd\w*|gaand\w*|lund\b|lauda\b|randi\b|harami\w*)", re.I)


def censor(text: str) -> str:
    """Strong words with their vowels masked: `fuck` -> `f*ck`, `chutiya` -> `ch*t*y*`."""
    def mask(m: "re.Match[str]") -> str:
        w = m.group(0)
        return w[0] + re.sub(r"[aeiouAEIOU\*]", "*", w[1:]) if len(w) > 1 else w

    return _MASK.sub(mask, text)


def to_output(cues: Sequence[Cue], pieces: List[Dict[str, Any]], dur: float) -> List[Cue]:
    """Cues in the stream's seconds -> the clip's own output seconds (through its retiming), each word too.

    A cue shorter than 0.2 s after the mapping (a freeze, a very fast ramp) is dropped; times are clamped to
    [0, dur].
    """
    def m(t: float) -> float:
        return min(max(edittimeline.src_to_out(pieces, t), 0.0), dur)

    out: List[Cue] = []
    for c in cues:
        s, e = m(c["s"]), m(c["e"])
        if e - s < 0.2:
            continue
        words = [{"t": w["t"], "s": round(m(w["s"]), 3), "e": round(m(w["e"]), 3)} for w in c["words"]]
        if c.get("off"):
            continue                                           # not drawn
        out.append({"s": round(s, 3), "e": round(e, 3), "text": c["text"], "words": words})
    return out


# --------------------------------------------------------------------------
# .ass (pure)
# --------------------------------------------------------------------------

DEFAULT_STYLE = {"size": 0.058, "color": "#FFFFFF", "highlight": "#FFB300", "outline": 0.0055, "x": 0.42, "y": 0.915, "width": 0.52,
                 "karaoke": True, "censor": True}


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return "&H{0:02X}{1}{2}{3}".format(alpha, b, g, r).upper()


def _ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    return "{0}:{1:02d}:{2:02d}.{3:02d}".format(cs // 360000, (cs // 6000) % 60, (cs // 100) % 60, cs % 100)


def _ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def ass_document(cues: Sequence[Cue], style: Dict[str, Any], canvas: Tuple[int, int], font_family: str) -> str:
    """The .ass file for one clip. Cue times are the clip's own output seconds. With `karaoke` each word carries a
    `\\k` duration: before it is said it is `color`, once said `highlight` - the word being spoken lights up."""
    W, H = canvas
    st = dict(DEFAULT_STYLE, **(style or {}))
    size = int(round(st["size"] * H))
    outline = max(2, int(round(st["outline"] * H)))
    left = int(round((st["x"] - st["width"] / 2.0) * W))
    right = int(round(W - (st["x"] + st["width"] / 2.0) * W))
    bottom = int(round((1.0 - st["y"]) * H))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: {0}".format(W), "PlayResY: {0}".format(H), "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Cap,{font},{size},{prim},{sec},&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,{out},{shadow},2,{ml},{mr},{mv},1".format(
            font=font_family, size=size, prim=_ass_color(st["highlight"]) if st["karaoke"] else _ass_color(st["color"]),
            sec=_ass_color(st["color"]), out=outline, shadow=max(1, outline // 3), ml=left, mr=right, mv=bottom),
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for c in cues:
        if st["karaoke"] and c.get("words"):
            body = ""
            t = c["s"]
            for w in c["words"]:
                word = censor(w["t"]) if st["censor"] else w["t"]
                gap = max(0.0, w["s"] - t)
                dur = max(0.01, w["e"] - max(w["s"], t))
                if gap > 0.005:
                    body += "{{\\k{0}}}".format(int(round(gap * 100)))       # a silent beat before the word
                body += "{{\\k{0}}}{1} ".format(int(round(dur * 100)), _ass_text(word))
                t = max(t, w["e"])
            text = body.strip()
        else:
            text = _ass_text(censor(c["text"]) if st["censor"] else c["text"])
        lines.append("Dialogue: 0,{0},{1},Cap,,0,0,0,,{{\\fad({2},{3})}}{4}".format(_ass_time(c["s"]), _ass_time(c["e"]), FADE_IN, FADE_OUT, text))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# generate (IO)
# --------------------------------------------------------------------------


def _client() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise StageError("The anthropic package is not installed.\nInstall with: pip install -U anthropic") from exc
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise StageError("ANTHROPIC_API_KEY is not set; captions are written by the model that also does `analyze`.")
    return anthropic.Anthropic()


def chunk_clip(client: Any, model: str, words: Sequence[Dict[str, Any]], max_tokens: int = 2000) -> List[Dict[str, Any]]:
    """One model call: the clip's words -> validated chunks."""
    resp = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": PROMPT + words_listing(words)}])
    text = "".join(getattr(b, "text", "") for b in resp.content)
    return parse_chunks(text, len(words))


def generate(settings: Settings, clips: Dict[str, Dict[str, Any]], client: Any = None,
             progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Cues for every clip. `clips` is `{clip_id: {"words": [...], "in": s, "out": s}}`; returns the `captions.json`
    document `{version, model, clips: {clip_id: {"cues": [...], "chunks": n, "fallback": bool}}}`. A clip whose
    model call fails (after one retry) falls back to the recogniser's own text and says so - it never aborts the rest."""
    model = str(settings.get("captions.model", "claude-sonnet-5-5"))
    min_conf = float(settings.get("captions.min_confidence", MIN_CONF))
    client = client or _client()
    workers = int(settings.get("captions.concurrency", 4))

    def one(item: Tuple[str, Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        cid, c = item
        words = c["words"]
        if not words:
            return cid, {"cues": [], "chunks": 0, "fallback": False}
        fallback, chunks, err = False, [], None
        for attempt in (1, 2):
            try:
                chunks = chunk_clip(client, model, words)
                if chunks:
                    break
            except Exception as exc:  # noqa: BLE001 - one clip failing must not stop the rest
                err = exc
        if not chunks:
            fallback, chunks = True, fallback_chunks(words)
            log.warning("captions for %s fell back to the recogniser's text (%s)", cid, err)
        return cid, {"cues": cues_from_chunks(chunks, words, c["in"], c["out"], min_conf=min_conf), "chunks": len(chunks), "fallback": fallback}

    out: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for cid, res in pool.map(one, list(clips.items())):
            out[cid] = res
            if progress:
                progress(cid)
    return {"version": SCHEMA, "model": model, "clips": out}
