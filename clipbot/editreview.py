"""The review loop: timestamped comments on a render, turned into spec changes by whoever reads them.

You watch a render and say "3:12 - the zoom is too much"; that note has to survive a re-render (the 3:12 of the next
version is a different frame), be readable by the CLI and by Claude, and be marked done once the change is made *and
seen*. So a comment is stored against what it was about, not only the second it was typed at:

    work/_edits/<name>/review.json
      {"schema": 1, "name": ..., "comments": [
         {"id": "n3", "t": 192.4, "on": "preview", "clip": "c4_22", "offset": 6.1, "chapter": "Teck vs. Helicopters",
          "kind": "change", "text": "zoom less", "status": "open", "added": "2026-10-25T10:12:03",
          "reply": "", "done": null}]}

`t` is seconds into the render named by `on` (`preview`, `final` or `v<N>`); `clip` and `offset` (seconds into that
clip) come from that render's timeline and are what to follow after the edit changes. `kind` is `change` (do this),
`taste` (a rule about how this person likes edits: it can become a taste rule), `question` (answer it in `reply`) or
`note` (nothing to do). `done` records `{at, in, how}`: which version fixed it and how, so "resolved" always names
something that can be looked at.

Pure functions (`parse_time`, `locate`, `add`, `resolve`...) over a dict, and a small IO layer for `review.json`.
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import edittimeline
from .specerror import SpecError

SCHEMA = 1
KINDS = ("change", "taste", "question", "note")
STATUSES = ("open", "done", "wontfix")
MAX_TEXT = 2000


def review_path(root: Path) -> Path:
    return Path(root) / "review.json"


def load(root: Path, name: str = "") -> Dict[str, Any]:
    p = review_path(root)
    if not p.exists():
        return {"schema": SCHEMA, "name": name, "comments": []}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SpecError("{0}: not valid JSON ({1})".format(p, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("comments"), list):
        raise SpecError("{0}: expected an object with a 'comments' list".format(p))
    return doc


def save(root: Path, doc: Dict[str, Any]) -> None:
    p = review_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def parse_time(value: Any) -> float:
    """`192.4`, `3:12`, `3:12.4` or `1:03:12` -> seconds."""
    if isinstance(value, (int, float)):
        t = float(value)
    else:
        text = str(value).strip()
        if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text):
            raise SpecError("a time looks like 192.4, 3:12 or 1:03:12 (got {0!r})".format(value))
        parts = text.split(":")
        t = 0.0
        for p in parts:
            t = t * 60.0 + float(p)
    if t < 0:
        raise SpecError("a time cannot be negative")
    return round(t, 2)


def mmss(t: float) -> str:
    t = max(0.0, float(t))
    return "{0}:{1:04.1f}".format(int(t // 60), t % 60) if t < 3600 else "{0}:{1:02d}:{2:02d}".format(int(t // 3600), int(t % 3600 // 60), int(t % 60))


def locate(timeline: Optional[Dict[str, Any]], t: float) -> Dict[str, Any]:
    """Where in the edit second `t` of a render is: `{clip, offset, chapter, label}` (all None without a timeline).
    The clip is the one on screen; during a cross-fade it is the later one."""
    if not timeline:
        return {"clip": None, "offset": None, "chapter": None, "label": None}
    hit = None
    for e in timeline.get("clips", []):
        if e["start"] - 1e-6 <= t < e["start"] + e["dur"] - 1e-6:
            hit = e
    if hit is None and timeline.get("clips"):
        last = timeline["clips"][-1]
        if t >= last["start"]:
            hit = last
    if hit is None:
        return {"clip": None, "offset": None, "chapter": None, "label": None}
    chapter = None
    for ch in edittimeline.chapters(timeline):
        if ch["t"] <= t + 1e-6:
            chapter = ch["title"]
    return {"clip": hit["id"], "offset": round(t - hit["start"], 2), "chapter": chapter, "label": hit.get("label")}


def _next_id(doc: Dict[str, Any]) -> str:
    nums = [int(c["id"][1:]) for c in doc["comments"] if str(c.get("id", "")).startswith("n") and str(c["id"])[1:].isdigit()]
    return "n{0}".format(max(nums or [0]) + 1)


def _find(doc: Dict[str, Any], cid: str) -> Dict[str, Any]:
    for c in doc["comments"]:
        if c.get("id") == cid:
            return c
    raise SpecError("no comment {0!r}".format(cid))


def add(doc: Dict[str, Any], t: Any, text: str, kind: str = "change", on: str = "preview",
        timeline: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Add a comment to the document (in place) and return it."""
    text = (text or "").strip()
    if not text:
        raise SpecError("a comment needs text")
    if len(text) > MAX_TEXT:
        raise SpecError("a comment is at most {0} characters".format(MAX_TEXT))
    if kind not in KINDS:
        raise SpecError("kind must be one of {0}".format(", ".join(KINDS)))
    if not re.fullmatch(r"preview|final|v\d+", on):
        raise SpecError("`on` is preview, final or v<N> (got {0!r})".format(on))
    secs = parse_time(t)
    where = locate(timeline, secs)
    c = {"id": _next_id(doc), "t": secs, "on": on, "clip": where["clip"], "offset": where["offset"], "chapter": where["chapter"],
         "kind": kind, "text": text, "status": "open", "added": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now if now is not None else time.time())),
         "reply": "", "done": None}
    doc["comments"].append(c)
    return c


def update(doc: Dict[str, Any], cid: str, text: Optional[str] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    c = _find(doc, cid)
    if text is not None:
        text = text.strip()
        if not text:
            raise SpecError("a comment needs text")
        c["text"] = text[:MAX_TEXT]
    if kind is not None:
        if kind not in KINDS:
            raise SpecError("kind must be one of {0}".format(", ".join(KINDS)))
        c["kind"] = kind
    return c


def resolve(doc: Dict[str, Any], cid: str, how: str = "", in_version: str = "", status: str = "done",
            now: Optional[float] = None) -> Dict[str, Any]:
    """Mark a comment done (or `wontfix`), recording how and in which version it was seen to be fixed."""
    if status not in ("done", "wontfix"):
        raise SpecError("status must be done or wontfix")
    c = _find(doc, cid)
    c["status"] = status
    c["done"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now if now is not None else time.time())),
                 "in": in_version, "how": (how or "").strip()}
    return c


def reopen(doc: Dict[str, Any], cid: str) -> Dict[str, Any]:
    c = _find(doc, cid)
    c["status"] = "open"
    c["done"] = None
    return c


def reply(doc: Dict[str, Any], cid: str, text: str) -> Dict[str, Any]:
    c = _find(doc, cid)
    c["reply"] = (text or "").strip()[:MAX_TEXT]
    return c


def delete(doc: Dict[str, Any], cid: str) -> None:
    c = _find(doc, cid)
    doc["comments"].remove(c)


def open_comments(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted([c for c in doc["comments"] if c.get("status", "open") == "open"], key=lambda c: (c.get("on", ""), c.get("t", 0.0)))


def line(c: Dict[str, Any]) -> str:
    """One comment as a line a terminal can show."""
    where = "  [{0}{1}]".format(c["clip"], " +{0:.1f}s".format(c["offset"]) if c.get("offset") is not None else "") if c.get("clip") else ""
    status = "" if c.get("status", "open") == "open" else "  ({0}{1})".format(c["status"], ": " + c["done"]["how"] if c.get("done") and c["done"].get("how") else "")
    return "{0:<4} {1:<7} {2:>8} on {3:<8}{4}  {5}{6}".format(c["id"], c["kind"], mmss(c["t"]), c.get("on", "?"), where, c["text"], status)


# --------------------------------------------------------------------------
# what the review page and the review commands show (IO over an edit's folder)
# --------------------------------------------------------------------------


def timeline_for(root: Path, name: str, which: str) -> Optional[Dict[str, Any]]:
    """The timeline `which` (`preview`, `final` or `v<N>`) was rendered with, or None when it was not kept (an edit
    archived by hand before versions were recorded has the mp4 and no timeline)."""
    rd = Path(root) / "render"
    if which in ("preview", "final"):
        p = rd / "{0}.{1}.timeline.json".format(name, which)
    elif re.fullmatch(r"v\d+", which):
        p = rd / which / "{0}.{1}.timeline.json".format(name, which)
    else:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("timeline")
    except (OSError, ValueError):
        return None


def _caption_texts(root: Path) -> Dict[str, str]:
    p = Path(root) / "captions.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {cid: " / ".join(c["text"] for c in v.get("cues", []) if not c.get("off")) for cid, v in (doc.get("clips") or {}).items()}


def _verify_flags(root: Path, which: str) -> List[Dict[str, str]]:
    tier = "preview" if which == "preview" else ("final" if which == "final" else None)
    if tier is None:
        return []
    try:
        doc = json.loads((Path(root) / "verify" / tier / "verify.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{"name": c["name"], "status": c["status"], "detail": c["detail"]} for c in doc.get("checks", []) if c["status"] != "pass"]


def overview(root: Path, name: str, which: Optional[str] = None, taste: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Everything the review page needs for one render: the versions that can be played, that render's chapters and
    clips (with the caption text each shows), what `verify` flagged, the comments, and the taste rules in force."""
    from . import editversions

    versions = [{k: v for k, v in x.items() if k != "file"} for x in editversions.list_versions(root, name)]
    have = [v["which"] for v in versions]
    if not which or which not in have:
        which = "preview" if "preview" in have else ("final" if "final" in have else (have[-1] if have else ""))
    tl = timeline_for(root, name, which) if which else None
    caps = _caption_texts(root)
    clips = []
    if tl:
        for e in tl.get("clips", []):
            clips.append({"id": e["id"], "type": e["type"], "label": e.get("label"), "start": round(e["start"], 2), "dur": round(e["dur"], 2),
                          "src": e.get("src"), "in": e.get("in"), "out": e.get("out"), "tags": e.get("tags", []),
                          "markers": len(e.get("markers") or []), "captions": caps.get(e["id"], "")})
    doc = load(root, name)
    return {"name": name, "which": which, "versions": versions,
            "duration": tl.get("duration") if tl else next((v["duration"] for v in versions if v["which"] == which), None),
            "chapters": edittimeline.chapters(tl) if tl else [], "clips": clips, "verify": _verify_flags(root, which),
            "comments": doc["comments"], "taste": [{"id": r["id"], "text": r["text"]} for r in taste]}
