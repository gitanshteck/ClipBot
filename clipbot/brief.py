"""The daily stream brief: data, page rendering and live on-air state.

The brief is a run of show for the start of each stream (talk, reactions, chat
games) plus game picks, written each day by a Claude session into
``<brief dir>/brief.json`` (see tools/stream-brief/DAILY.md). This module is
what both front ends share:

- ``tools/stream-brief/build_page.py`` renders the page into one self-contained
  HTML file for the claude.ai artifact (``backend="claude"``);
- the dashboard (``clipbot/server/app.py``) renders the same page live at
  ``/brief`` (``backend="local"``) and serves the OBS overlays and dock, which
  read the live on-air state kept here.

Live state, markers and feedback are plain JSON files under the brief dir, so
the daily Claude run can read what happened on stream without any database:

    <brief dir>/live/state.json          on-air state (segment, item, counters)
    <brief dir>/markers/<date>.json      segment changes and clip presses
    <brief dir>/feedback.json            more / less / drop per brief item

The brief dir is ``brief.dir`` in settings (env ``CLIPBOT_BRIEF_DIR``), else
``<work_root>/_brief``. The underscore keeps it out of the workspace listing.
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PROJECT_ROOT, Settings

ASSETS = Path(__file__).resolve().parent / "server" / "static" / "brief"

REQUIRED = {
    "date": str, "weekday": str, "edition": int, "generated_at": str, "headline": dict,
    "go_live": dict, "platforms": list, "run": list, "docket": list, "keep_alive": list,
    "clip_targets": list, "games": dict, "week": list, "calendar": list, "evidence": dict,
    "sources": list,
}
SEGMENT_KEYS = ("id", "t", "dur", "name", "kind", "clip", "screen")
KINDS = {"starting", "talk", "chat", "react", "transition", "game"}
SCENES = {"starting", "card", "topic", "guess", "vote", "game", "next"}
FRAMES = ("chat", "game", "vertical", "full")
CARD_MAX = 48
# Things that must never reach a page shown live on stream. A backstop, not the filter.
PRIVATE = re.compile(r"\bvisa\b|\bemployer\b|passport|@gmail|notion\.so|\+61 ?4|\+91 ?[6-9]", re.I)

MARKER_KINDS = {"segment", "clip"}
REACTIONS = {"more", "less", "drop"}
FEEDBACK_KINDS = {"docket", "segment", "game", "scene"}
STATE_MAX_BYTES = 8192
_TEXT_MAX = 200

_LOCK = threading.RLock()


# --- locations -------------------------------------------------------------


def brief_dir(settings: Settings) -> Path:
    value = str(settings.get("brief.dir", "") or "").strip()
    if value:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path
    return settings.work_root / "_brief"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --- the brief -------------------------------------------------------------


def problems(brief: Dict[str, Any]) -> List[str]:
    """Everything that would stop the page rendering properly, as readable lines."""
    out = []
    for key, typ in REQUIRED.items():
        if not isinstance(brief.get(key), typ):
            out.append("missing or wrong type: {0}".format(key))
    docket_ids = {d.get("id") for d in brief.get("docket", []) if isinstance(d, dict)}
    for i, seg in enumerate(brief.get("run", []) or []):
        if not isinstance(seg, dict):
            out.append("run[{0}] is not an object".format(i))
            continue
        for k in SEGMENT_KEYS:
            if k not in seg:
                out.append("run[{0}] lacks {1}".format(i, k))
        if seg.get("kind") not in KINDS:
            out.append("run[{0}] kind {1!r} not one of {2}".format(i, seg.get("kind"), sorted(KINDS)))
        for k in ("title", "sub", "prompt"):
            if k not in (seg.get("screen") or {}):
                out.append("run[{0}].screen lacks {1}".format(i, k))
        for ref in seg.get("items", []) or []:
            if ref not in docket_ids:
                out.append("run[{0}] points at docket item {1!r} that does not exist".format(i, ref))
        scene = seg.get("scene")
        if scene is not None and scene not in SCENES:
            out.append("run[{0}] scene {1!r} not one of {2}".format(i, scene, sorted(SCENES)))
        if scene == "guess":
            rounds = seg.get("rounds") or []
            if not rounds or not all(isinstance(r, dict) and "q" in r and "a" in r for r in rounds):
                out.append("run[{0}] guess scene needs rounds with q and a".format(i))
        if scene == "vote" and len(seg.get("poll") or []) < 2:
            out.append("run[{0}] vote scene needs a poll with at least 2 options".format(i))
    for d in brief.get("docket", []) or []:
        if not isinstance(d, dict):
            continue
        if len(d.get("card", "") or "") > CARD_MAX:
            out.append("docket {0}: card title over {1} characters (it must read in one glance on stream)".format(d.get("id"), CARD_MAX))
        if "poll" in d and not (2 <= len(d["poll"] or []) <= 3):
            out.append("docket {0}: poll needs 2 or 3 options".format(d.get("id")))
    if brief.get("run") and not any(isinstance(s, dict) and s.get("kind") == "game" for s in brief["run"]):
        out.append("run has no game segment")
    text = json.dumps(brief, ensure_ascii=False)
    for m in sorted(set(PRIVATE.findall(text))):
        out.append("private-looking word on a page meant for stream: {0!r}".format(m))
    return out


def load_brief(settings: Settings) -> Dict[str, Any]:
    """Today's brief with live Steam player counts merged into the ranked games.

    Raises FileNotFoundError when no brief has been written yet.
    """
    root = brief_dir(settings)
    path = root / "brief.json"
    if not path.is_file():
        raise FileNotFoundError("No brief yet: {0}".format(path))
    brief = json.loads(path.read_text(encoding="utf-8"))
    return merge_steam(brief, _read_json(root / "steam_ccu.json", None))


def merge_steam(brief: Dict[str, Any], ccu: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not ccu or not isinstance(ccu.get("games"), dict):
        return brief
    by_id = {str(v.get("appid")): v.get("players") for v in ccu["games"].values() if isinstance(v, dict)}
    for g in (brief.get("games") or {}).get("ranked", []) or []:
        if str(g.get("appid")) in by_id:
            g["steam_players"] = by_id[str(g.get("appid"))]
    brief.setdefault("games", {})["steam_fetched_at"] = ccu.get("fetched_at")
    return brief


def render_page(brief: Dict[str, Any], backend: str, local_base: str = "http://127.0.0.1:8765") -> str:
    """The rundown page as one HTML document: shared scene code and data inlined.

    ``backend`` is "claude" (the artifact: claude.ai database, room and comments)
    or "local" (this PC's dashboard: /api/brief/*).
    """
    if backend not in ("claude", "local"):
        raise ValueError("backend must be 'claude' or 'local'")
    page = (ASSETS / "page.html").read_text(encoding="utf-8")
    data = dict(brief)
    data["backend"] = backend
    data["local_base"] = local_base.rstrip("/")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    pieces = {
        "/*__SCENES_CSS__*/": (ASSETS / "scenes.css").read_text(encoding="utf-8"),
        "/*__SCENES_JS__*/": (ASSETS / "scenes.js").read_text(encoding="utf-8").replace("</script", "<\\/script"),
        "/*__LIVE_JS__*/": (ASSETS / "live.js").read_text(encoding="utf-8").replace("</script", "<\\/script"),
    }
    for marker, text in pieces.items():
        if marker not in page:
            raise ValueError("page.html has no {0} placeholder".format(marker))
        page = page.replace(marker, text)
    if "__BRIEF_JSON__" not in page:
        raise ValueError("page.html has no __BRIEF_JSON__ placeholder")
    return page.replace("__BRIEF_JSON__", payload)


# --- live on-air state -----------------------------------------------------


def default_state(date: str = "") -> Dict[str, Any]:
    return {"v": 0, "on": False, "seg": 0, "item": 0, "reveal": False, "kills": 0, "deaths": 0,
            "startedAt": 0, "pausedAt": 0, "pausedTotal": 0, "bg": "brand", "lay": "h",
            "l3only": False, "ask": "", "askShow": False, "date": date}


def read_state(settings: Settings, date: str = "") -> Dict[str, Any]:
    state = _read_json(brief_dir(settings) / "live" / "state.json", None)
    if not isinstance(state, dict) or (date and state.get("date") != date):
        return default_state(date)
    return state


def write_state(settings: Settings, incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Store ``incoming`` if it is newer than what is stored; return what is stored now.

    Every view sends the whole state with ``v`` (a millisecond timestamp of the
    change), so the newest change wins whichever window made it. Raises
    ValueError for a body that is not a small state object.
    """
    if not isinstance(incoming, dict):
        raise ValueError("state must be an object")
    if len(json.dumps(incoming)) > STATE_MAX_BYTES:
        raise ValueError("state is too large")
    if not isinstance(incoming.get("v"), (int, float)) or isinstance(incoming.get("v"), bool):
        raise ValueError("state needs a numeric v")
    if not isinstance(incoming.get("date"), str):
        raise ValueError("state needs a date")
    for key in ("seg", "item", "kills", "deaths"):
        value = incoming.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 100000:
            raise ValueError("state.{0} must be a small whole number".format(key))
    clean = default_state(incoming["date"])
    clean.update({k: incoming[k] for k in clean if k in incoming})
    if clean["bg"] not in ("brand", "green", "clear"):
        raise ValueError("state.bg must be brand, green or clear")
    if clean["lay"] not in ("h", "v"):
        raise ValueError("state.lay must be h or v")
    if not isinstance(clean["ask"], str) or not re.match(r"^[a-z0-9-]{0,40}$", clean["ask"]):
        raise ValueError("state.ask must be a chat id")
    clean["askShow"] = bool(clean["askShow"])
    path = brief_dir(settings) / "live" / "state.json"
    with _LOCK:
        current = _read_json(path, None)
        if isinstance(current, dict) and current.get("date") == clean["date"] and current.get("v", 0) >= clean["v"]:
            return current
        _write_json(path, clean)
        return clean


# --- markers and feedback --------------------------------------------------


def _text(value: Any, limit: int = _TEXT_MAX) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value if value is not None else ""))[:limit]


def _date(value: Any) -> str:
    value = str(value or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ValueError("date must be YYYY-MM-DD")
    return value


def add_marker(settings: Settings, doc: Dict[str, Any]) -> Dict[str, Any]:
    """Append one marker to ``markers/<date>.json`` and return it as stored."""
    if not isinstance(doc, dict):
        raise ValueError("marker must be an object")
    kind = doc.get("kind")
    if kind not in MARKER_KINDS:
        raise ValueError("marker kind must be one of {0}".format(sorted(MARKER_KINDS)))
    date = _date(doc.get("date"))
    marker = {
        "kind": kind,
        "seg_id": _text(doc.get("seg_id"), 80),
        "seg_name": _text(doc.get("seg_name"), 120),
        "item": _text(doc.get("item"), 80),
        "label": _text(doc.get("label")),
        "edition": doc.get("edition") if isinstance(doc.get("edition"), int) else None,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = brief_dir(settings) / "markers" / "{0}.json".format(date)
    with _LOCK:
        data = _read_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get("markers"), list):
            data = {"date": date, "markers": []}
        data["markers"].append(marker)
        _write_json(path, data)
    return marker


def read_markers(settings: Settings, date: str) -> Dict[str, Any]:
    data = _read_json(brief_dir(settings) / "markers" / "{0}.json".format(_date(date)), None)
    return data if isinstance(data, dict) else {"date": date, "markers": []}


def set_feedback(settings: Settings, doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Record (or with ``reaction: null`` clear) one reaction. Returns the stored row."""
    if not isinstance(doc, dict):
        raise ValueError("feedback must be an object")
    date = _date(doc.get("date"))
    kind = doc.get("kind")
    if kind not in FEEDBACK_KINDS:
        raise ValueError("feedback kind must be one of {0}".format(sorted(FEEDBACK_KINDS)))
    target = _text(doc.get("target"), 80)
    if not target:
        raise ValueError("feedback needs a target")
    reaction = doc.get("reaction")
    if reaction is not None and reaction not in REACTIONS:
        raise ValueError("reaction must be one of {0} or null".format(sorted(REACTIONS)))
    key = "{0}_{1}_{2}".format(date, kind, target)
    path = brief_dir(settings) / "feedback.json"
    with _LOCK:
        data = _read_json(path, None)
        if not isinstance(data, dict):
            data = {}
        if reaction is None:
            data.pop(key, None)
            row = None
        else:
            row = {"date": date, "edition": doc.get("edition") if isinstance(doc.get("edition"), int) else None,
                   "kind": kind, "target": target, "label": _text(doc.get("label")), "reaction": reaction,
                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            data[key] = row
        _write_json(path, data)
    return row


def list_feedback(settings: Settings, date: Optional[str] = None) -> List[Dict[str, Any]]:
    data = _read_json(brief_dir(settings) / "feedback.json", {})
    rows = [r for r in data.values() if isinstance(r, dict)] if isinstance(data, dict) else []
    if date:
        rows = [r for r in rows if r.get("date") == date]
    return sorted(rows, key=lambda r: r.get("at", ""))
