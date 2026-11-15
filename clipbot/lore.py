"""Lore: what the on-stream AI knows about past streams, running bits and recent events.

A private pack in ``<brief dir>/lore/`` (gitignored with the rest of ``work/``):

- ``lore.md``      who he is on stream, running bits, regulars, callbacks, a "never state
                   on stream" list and answer rules. Hand-editable: the files are the truth.
- ``recent.md``    a short digest of the last few streams and the open threads.
- ``streams.json`` one record per past stream: date, title, games, topics, moments
                   (minute + what), a few of his own lines. Searched per question.

``context(settings, question)`` returns one block for the model prompt: the lore and the
recent digest (capped) plus the few stream moments that match the question. Nothing here
calls a model or the network; it is cheap enough to run on every question. Whatever is in
these files may end up paraphrased on stream, so the block tells the model to treat the
private list as never-to-say. A missing pack is not an error: ``context`` returns "".
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .brief import brief_dir
from .config import Settings

LORE_DIRNAME = "lore"
LORE_MAX_CHARS = 9000      # about 2,000 tokens
RECENT_MAX_CHARS = 3500
HITS_MAX = 4
HIT_MIN_SCORE = 2.0
STOPWORDS = frozenset(
    "the a an and or but of to in on at for with from by is are was were be been it this that these those i you he she we they me my "
    "your his her our their what when where why how who which did do does done have has had not no yes can could would should will "
    "about tell remember last time again ever any some more most just like get got into out over then than also very really "
    "kya hai tha thi the kab kaun kyu kyun kaise kitna kitne kahan mera meri mere tera teri tere ka ki ke ko se me mein par aur ya".split())

PREAMBLE = (
    "Background about the streamer and his channel (private context for you, not something to recite).\n"
    "Use it quietly: a callback, a name, a date, a running bit. Do not quote long passages. If the background does not\n"
    "cover what he asks, say you do not remember rather than guessing. Anything under 'Never state on stream' must not\n"
    "appear in your answer in any form, even when asked directly.")


def lore_dir(settings: Settings) -> Path:
    return brief_dir(settings) / LORE_DIRNAME


def _read(path: Path, cap: int) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    return cut[:cut.rfind("\n")] if "\n" in cut else cut   # end on a whole line


def load_streams(settings: Settings) -> List[Dict[str, Any]]:
    path = lore_dir(settings) / "streams.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']{3,}", str(text).lower()) if w not in STOPWORDS]


def search(streams: List[Dict[str, Any]], query: str, limit: int = HITS_MAX) -> List[Dict[str, Any]]:
    """The stream moments, topics and lines that best match ``query``, newest first on ties."""
    words = set(tokens(query))
    if not words:
        return []
    scored = []
    for rank, stream in enumerate(streams):          # the file is newest first
        date, title = str(stream.get("date", "")), str(stream.get("title", ""))
        games = " ".join(str(g) for g in stream.get("games") or [])
        base = len(words & set(tokens(title + " " + games))) * 0.5
        base += len(words & set(tokens(" ".join(str(t) for t in stream.get("topics") or [])))) * 0.75
        entries = [("moment", m.get("what", ""), m.get("t_min")) for m in stream.get("moments") or [] if isinstance(m, dict)]
        entries += [("line", ln, None) for ln in stream.get("lines") or []]
        for kind, text, minute in entries:
            hit = len(words & set(tokens(text)))
            score = hit + (base if hit else 0.0)
            if score >= HIT_MIN_SCORE or (hit and len(words) == 1):
                scored.append((score, -rank, {"date": date, "title": title, "kind": kind, "text": str(text), "t_min": minute}))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [row for _s, _r, row in scored[:limit]]


def _hit_line(hit: Dict[str, Any]) -> str:
    when = hit["date"] + (" at ~{0} min".format(int(hit["t_min"])) if isinstance(hit.get("t_min"), (int, float)) else "")
    prefix = "he said" if hit["kind"] == "line" else "happened"
    return "- {0} ({1}): {2}: {3}".format(when, hit["title"][:60], prefix, hit["text"][:220])


def context(settings: Settings, question: str = "") -> str:
    """The background block for a prompt, or "" when there is no lore pack (or it is switched off)."""
    if not settings.get("brief.chat.lore", True):
        return ""
    root = lore_dir(settings)
    lore = _read(root / "lore.md", LORE_MAX_CHARS)
    recent = _read(root / "recent.md", RECENT_MAX_CHARS)
    hits = search(load_streams(settings), question) if question else []
    if not (lore or recent or hits):
        return ""
    parts = [PREAMBLE]
    if lore:
        parts += ["", "Channel lore:", lore]
    if recent:
        parts += ["", "Recent streams:", recent]
    if hits:
        parts += ["", "Past moments that match this question:"] + [_hit_line(h) for h in hits]
    return "\n".join(parts)
