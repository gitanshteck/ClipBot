"""Word-level timing for the edit engine: where each spoken word starts and ends.

A transcript line is good to about two seconds, which is the wrong resolution for a cut, a caption or a
profanity check. `audio.wav` is transcribed again - only the windows an edit uses, not the whole stream - with
word timestamps (`whisper-1`, the project's default transcription model, which returns them), and the words
are stored per workspace and shared between edits.

Whisper's word times are good enough to cut and caption by *once they are corrected against the sound*, which
this module does and measures. Over three real clips (Hindi over game audio) 91-100% of word starts had the
signal up within 60 ms and 0-10% of words sat mostly in silence - but two faults are common and handled:

* **collapsed words**: a run of words all given the same instant (`बाद[8.36-8.36] नहीं[8.36-8.36]`). `repair`
  spreads them over the gap by length and marks them `est`.
* **words stretched over a pause**: `हैं[10.06-11.50]` is a three-letter word held for 1.4 s. Each word gets
  *voiced runs* (`vi`, with `vs`/`ve` their outer edges): where the audio is up, found against the window's own
  loudness. Against the audio at the cut itself, 11 of the 17 cuts this module flagged in a real episode were
  mid-speech and 6 were in a dip - which is why a cut is "inside" only where the sound is.
  A cut is judged against the voiced span, so a pause inside a long word is a legitimate place to cut.

**The language is not pinned to Hindi here.** The project's transcript pins `language: "hi"` (right for a stream that is
mostly Hindi), but a Hinglish streamer switches to English mid-sentence, and on a window of English speech `hi` returned
Devanagari nonsense (`इ कि जो हुड़ा है भे मुझे पинки मर`) where auto-detect returned `wait bro wait, stop doing that bro`;
on a window of loud game audio it returned `Like share and subscribe`. Measured on ten real clips: `hi` and auto both
write Devanagari for Hinglish and misread it; auto with a short *Roman-script Hinglish prompt* (`HINGLISH_PROMPT`)
writes it the way people type it (`mere pe mortar gir gaya, bro bro, help!`) and was right or better on 8 of 10, no
worse on the others. So words are fetched with `words.language` (default auto) and `words.prompt`; a window records
which it was fetched with, and one fetched under other settings is not reused. A prompt can come back as the
"transcript" of silence, so a window whose text is the prompt is treated as no speech.

Pure functions (`repair`, `voiced_spans`, `inside_word`, `word_snap`, `strong_words`) and the IO that fetches
(`words_for`). The store is `work/_cache/edit/words/<workspace>.json`; nothing here runs at render time - the
renderer reads only what `clipbot edit words` already fetched.
"""

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import editcut
from .config import Settings
from .utils import StageError, get_logger, resolve_tool
from .workspace import Workspace

log = get_logger(__name__)

PAD = 2.0                 # seconds fetched either side of a clip, so words at its edges are whole
MIN_WORD = 0.04           # a word shorter than this was given no time at all
PRICE_PER_MINUTE = 0.006  # whisper-1, for the estimate printed before anything is sent
STORE_VERSION = 2
HINGLISH_PROMPT = "Yaar bhai, wait wait, ye kya ho gaya? Chal na, main reposition karta hoon. Oh my god, kya baat hai!"
IN_ROOM = 0.15            # an in point lands this long before the word it moved to
OUT_TAIL = 0.12           # an out point lands this long after the word it moved past
EDGE = 0.06               # a cut this close to a word's edge is not "inside" it

Word = Dict[str, Any]


# --------------------------------------------------------------------------
# repairing and measuring words (pure)
# --------------------------------------------------------------------------


def repair(words: Sequence[Word], min_dur: float = MIN_WORD) -> List[Word]:
    """Words with every collapsed run spread over the time it must have taken.

    A word whose end is within `min_dur` of its start has been given no time; a run of them is spread
    across [the run's start, the next real word's start] in proportion to their length, and marked
    `est: True`. Words keep their order and never overlap their neighbours.
    """
    ws = [dict(w) for w in words if str(w.get("w", "")).strip()]
    i = 0
    while i < len(ws):
        if ws[i]["e"] - ws[i]["s"] >= min_dur:
            i += 1
            continue
        j = i
        while j < len(ws) and ws[j]["e"] - ws[j]["s"] < min_dur:
            j += 1
        start = max(ws[i]["s"], ws[i - 1]["e"] if i else ws[i]["s"])
        # the run ends where the next real word starts; if that word starts at the same instant (the recogniser
        # put the whole phrase's time on its last word) the run shares that word's time
        last = j
        if j < len(ws) and ws[j]["s"] - start < min_dur * (j - i):
            last = j + 1
            end = ws[j]["e"]
        else:
            end = ws[j]["s"] if j < len(ws) else ws[j - 1]["e"] + 0.4 * (j - i)
        span = max(end - start, 0.0)
        if span >= min_dur * (last - i):
            weights = [max(1, len(re.sub(r"\W", "", ws[k]["w"]))) for k in range(i, last)]
            tot = float(sum(weights))
            t = start
            for k, wgt in zip(range(i, last), weights):
                ws[k]["s"] = round(t, 3)
                t += span * wgt / tot
                ws[k]["e"] = round(t, 3)
                ws[k]["est"] = True
        else:
            for k in range(i, j):
                ws[k]["est"] = True
        i = last
    return ws


def voiced_spans(words: Sequence[Word], times: Any, db: Any) -> List[Word]:
    """Add `vs`/`ve` to each word: the part of its span where the audio is up.

    `times`/`db` are an envelope (`editcut.read_envelope`) covering the words. "Up" is measured against
    the window's own range - 35% of the way from its quiet floor (15th percentile) to its loud level (90th)
    - because the game audio under the voice differs from stream to stream. A word that is never up is
    `silent: True` (a word the recogniser heard where there was none: it is not a place a cut can be inside).
    """
    import numpy as np

    out = [dict(w) for w in words]
    if len(db) < 10:
        for w in out:
            w["vs"], w["ve"] = w["s"], w["e"]
        return out
    floor, peak = np.percentile(db, 15), np.percentile(db, 90)
    thr = floor + 0.35 * (peak - floor)
    for w in out:
        m = np.nonzero((times >= w["s"] - 1e-6) & (times <= w["e"] + 1e-6) & (db > thr))[0]
        if len(m) == 0:
            w["vs"], w["ve"], w["silent"] = w["s"], w["s"], True
            continue
        # the runs where the audio is up, bridging dips under 80 ms (a word has them): a cut is "inside" the word
        # only where the sound is - a long dip in a stretched word is a place a cut can go
        runs: List[List[float]] = []
        for k in m:
            t0, t1 = float(times[k]), float(times[k]) + 0.01
            if runs and t0 - runs[-1][1] <= 0.08:
                runs[-1][1] = t1
            else:
                runs.append([t0, t1])
        runs = [[round(a, 3), round(b + 0.02, 3)] for a, b in runs]
        w["vs"], w["ve"], w["vi"] = runs[0][0], runs[-1][1], runs
        if sum(b - a for a, b in runs) < 0.06:           # up for an instant at its edge: the next word's onset, not this word
            w["silent"] = True
    return out


def inside_word(words: Sequence[Word], t: float, edge: float = EDGE) -> Optional[Word]:
    """The word a cut at `t` would split, or None. Judged on the voiced span: a cut in a pause the
    recogniser stretched a word over is fine. A cut within `edge` of a word's start or end is not inside it."""
    for w in words:
        if w.get("silent"):
            continue
        for s, e in (w.get("vi") or [[w.get("vs", w["s"]), w.get("ve", w["e"])]]):
            if s + edge < t < e - edge:
                return w
    return None


def word_snap(words: Sequence[Word], t: float, kind: str, max_move: float = 0.6) -> Dict[str, Any]:
    """Move a cut that would split a word to the edge of that word: an `in` to just before it, an `out` to
    just after. Returns {t, moved, inside, snapped}; `snapped` is False (and t unchanged) when the cut is not
    inside a word, or the move would be longer than `max_move` (the cut stays and `inside` names the word)."""
    if kind not in ("in", "out"):
        raise ValueError("kind must be 'in' or 'out'")
    cur = t
    hit = inside_word(words, cur)
    if hit is None:
        return {"t": round(t, 3), "moved": 0.0, "inside": None, "snapped": False}
    first = hit
    for _ in range(4):                      # moving past one word can land in the next
        ws = [w for w in words if not w.get("silent")]
        idx = ws.index(hit)
        if kind == "in":
            prev_end = ws[idx - 1].get("ve", ws[idx - 1]["e"]) if idx else -1e9
            cur = max(prev_end + 0.02, hit.get("vs", hit["s"]) - IN_ROOM)
        else:
            nxt_start = ws[idx + 1].get("vs", ws[idx + 1]["s"]) if idx + 1 < len(ws) else 1e9
            cur = min(nxt_start - 0.02, hit.get("ve", hit["e"]) + OUT_TAIL)
        hit2 = inside_word(words, cur)
        if hit2 is None:
            break
        hit = hit2
    if abs(cur - t) > max_move or inside_word(words, cur) is not None:
        return {"t": round(t, 3), "moved": 0.0, "inside": first["w"], "snapped": False}
    return {"t": round(cur, 3), "moved": round(cur - t, 3), "inside": first["w"], "snapped": True}


# strong language, in the scripts the transcripts come in. A warning ("check the first 30 s"), not a filter, so it
# errs towards flagging - but a short Latin word must be a whole word ("chut" is in "parachute").
_STEMS_LATIN = ("fuck", "f**k", "fck", "shit", "bitch", "bastard", "asshole", "chutiy", "behenchod", "bhenchod", "behen ki", "bhosd", "bhosad",
                "madarchod", "maderchod", "harami", "motherfuck")
_WORDS_LATIN = ("chut", "dick", "pussy", "cunt", "mc", "bc", "gaand", "gand", "lund", "lauda", "randi", "kamina", "kutte")
STRONG_DEVANAGARI = ("चूत", "चुत", "चूथ", "चूतिया", "चोद", "चुद", "बहन की", "बेहन की", "भोस", "मादर", "गांड", "गाँड", "लंड", "लौड", "रंडी",
                     "हरामी", "कमीन", "फक", "शिट", "बिच", "बास्टर")
_LATIN_RE = re.compile(r"(?<![a-z])(?:{0})|(?<![a-z])(?:{1})(?![a-z])".format(
    "|".join(re.escape(k) for k in _STEMS_LATIN), "|".join(re.escape(k) for k in _WORDS_LATIN)))


def strong_words(text: str) -> List[str]:
    """The strong-language fragments found in `text` (empty when none)."""
    low = text.lower()
    found = [m.group(0) for m in _LATIN_RE.finditer(low)]
    return found + [k for k in STRONG_DEVANAGARI if k in low]


# --------------------------------------------------------------------------
# the store and the fetch (IO)
# --------------------------------------------------------------------------


def store_path(settings: Settings, ws: Workspace) -> Path:
    return Path(settings.work_root) / "_cache" / "edit" / "words" / "{0}.json".format(ws.slug)


def _load_store(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("version") == STORE_VERSION:
                return doc
        except (ValueError, OSError):
            pass
    return {"version": STORE_VERSION, "windows": []}


def _save_store(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def estimate_cost(seconds: float) -> float:
    return seconds / 60.0 * PRICE_PER_MINUTE


def _client(settings: Settings) -> Any:
    from .stages import transcribe_openai

    return transcribe_openai._client(settings)


def _params(settings: Settings) -> Dict[str, Any]:
    """The recogniser settings a window is fetched under: `words.model`, `words.language` (default auto) and
    `words.prompt` (default `HINGLISH_PROMPT`; an empty string sends none)."""
    language = settings.get("words.language", None)
    language = None if language is None or str(language).lower() in ("", "auto", "none", "null") else str(language)
    prompt = settings.get("words.prompt", HINGLISH_PROMPT)
    return {"model": str(settings.get("words.model", "whisper-1")), "language": language, "prompt": str(prompt or "")}


def signature(settings: Settings) -> str:
    """A short id for `_params`: a stored window is reused only under the settings it was fetched with."""
    p = _params(settings)
    return hashlib.sha1(json.dumps(p, sort_keys=True).encode("utf-8")).hexdigest()[:10]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ]+", "", text.lower())


def echoes_prompt(text: str, prompt: str) -> bool:
    """True when `text` is the prompt coming back as the transcript - what a prompted recogniser can do with silence."""
    t, p = _norm(text), _norm(prompt)
    if not t or not p:
        return False
    return (t in p and len(t) >= min(len(p), 12)) or (p in t and len(t) <= 1.25 * len(p))


def fetch_window(settings: Settings, ws: Workspace, a: float, b: float, client: Any = None) -> Dict[str, Any]:
    """Transcribe [a, b] of a workspace's `audio.wav` with word timestamps and return the window record
    `{a, b, model, sig, words}` (words repaired and given voiced spans; times in the stream's own seconds)."""
    if not ws.audio_path.exists():
        raise StageError("{0} has no audio.wav; run the audio stage first".format(ws.slug))
    ffmpeg = resolve_tool(settings.tool("ffmpeg"), "install ffmpeg")
    params = _params(settings)
    model, language, prompt = params["model"], params["language"], params["prompt"]
    scratch = Path(settings.work_root) / "_cache" / "edit" / "words" / "_tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    mp3 = scratch / "{0}-{1:.0f}-{2:.0f}.mp3".format(ws.slug[:20], a * 10, b * 10)
    proc = subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", "{0:.3f}".format(a), "-t", "{0:.3f}".format(b - a), "-i", str(ws.audio_path),
                           "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(mp3)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=300)
    if proc.returncode != 0:
        raise StageError("ffmpeg could not cut {0}: {1}".format(ws.slug, proc.stderr[-300:]))
    client = client or _client(settings)
    kwargs: Dict[str, Any] = {"model": model, "response_format": "verbose_json", "timestamp_granularities": ["word"]}
    if language:
        kwargs["language"] = language
    if prompt:
        kwargs["prompt"] = prompt
    last: Optional[Exception] = None
    resp = None
    try:
        for attempt in range(1, 4):
            try:
                with mp3.open("rb") as fh:
                    resp = client.audio.transcriptions.create(file=fh, **kwargs)
                break
            except Exception as exc:  # noqa: BLE001 - network/API: retry, then say so
                last = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
    finally:
        if mp3.exists():
            mp3.unlink()
    if resp is None:
        raise StageError("the word-timing request for {0} {1:.0f}-{2:.0f}s failed: {3}".format(ws.slug, a, b, last))
    raw = [{"w": w.word, "s": round(float(w.start) + a, 3), "e": round(float(w.end) + a, 3)} for w in (getattr(resp, "words", None) or [])]
    raw = [r for r in raw if any(ch.isalnum() for ch in r["w"])]      # 🔥🔥🔥 is what a recogniser writes for game noise, not a word
    if prompt and echoes_prompt(" ".join(r["w"] for r in raw), prompt):
        log.info("%s %.0f-%.0fs: the recogniser returned the prompt (no speech); no words kept", ws.slug, a, b)
        raw = []
    words = repair(raw)
    try:
        times, db = editcut.read_envelope(ws.audio_path, a, b)
        words = voiced_spans(words, times, db)
    except StageError:
        pass
    return {"a": round(a, 3), "b": round(b, 3), "model": model, "sig": signature(settings), "words": words}


def words_for(settings: Settings, ws: Workspace, a: float, b: float, fetch: bool = True, client: Any = None,
              pad: float = PAD) -> Optional[List[Word]]:
    """The words spoken in [a, b] of a workspace, fetching (and storing) a window around them if no stored
    window covers it by `pad`/2. Returns None when there are none stored and `fetch` is False."""
    path = store_path(settings, ws)
    doc = _load_store(path)
    need_a, need_b = a - pad / 4.0, b + pad / 4.0
    sig = signature(settings)
    win = next((w for w in doc["windows"] if w.get("sig") == sig and w["a"] <= need_a and w["b"] >= need_b), None)
    if win is None:
        if not fetch:
            return None
        win = fetch_window(settings, ws, max(0.0, a - pad), b + pad, client=client)
        doc["windows"] = [w for w in doc["windows"] if w.get("sig") == sig] + [win]   # windows from other settings are stale
        _save_store(path, doc)
    return [w for w in win["words"] if w["e"] >= a - 0.3 and w["s"] <= b + 0.3]
