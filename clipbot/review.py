"""Per-clip review state, stored in a sidecar `clips.json`.

Why a sidecar rather than fields on `candidates.json`: re-running the analysis
stage with an edited rubric rewrites `candidates.json` wholesale, and that is the
core tuning loop. Review state kept in that file would be destroyed every time
the rubric changed. Keeping candidates immutable also preserves what
`rubric_sha1` is for - "these picks came from that version of my criteria".

`clips.json` shape:

    {
      "version": 1,
      "updated_at": 1785770827.5,
      "next_id": 3,
      "source": {"candidates_sha1": "...", "rubric_sha1": "...", "model": "..."},
      "clips": [
        {
          "id": "c_0001",
          "origin": "analyze" | "manual",
          "source_start": 338.9, "source_end": 426.5,   # what the model said
          "start": 337.4, "end": 428.0,                 # what the human wants
          "status": "pending" | "approved" | "rejected" | "cut" | "failed",
          "rating": 0, "title": "", "description": "", "why": "",
          "tags": [], "notes": "",
          "stale": false, "orphaned": false,
          "output": {...} | null,
          "created_at": ..., "updated_at": ...
        }
      ]
    }
"""

import hashlib
import time
from typing import Any, Dict, List, Optional

from .utils import get_logger
from .workspace import Workspace

log = get_logger(__name__)

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CUT = "cut"
STATUS_FAILED = "failed"

VALID_STATUSES = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_CUT,
    STATUS_FAILED,
)

# Below this overlap, a re-analysed candidate is treated as a different moment
# rather than a moved version of an existing one.
MATCH_IOU_THRESHOLD = 0.5

# How far the model's range may drift before we badge the clip as stale.
STALE_DRIFT_SECONDS = 2.0

EDITABLE_FIELDS = ("status", "rating", "title", "tags", "notes", "start", "end", "reel")


def _empty_doc() -> Dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": time.time(),
        "next_id": 1,
        "source": {},
        "clips": [],
    }


def load(ws: Workspace) -> Dict[str, Any]:
    """Read clips.json, or an empty document if it doesn't exist yet."""
    path = ws.clips_path
    if not path.exists():
        return _empty_doc()
    try:
        doc = ws.read_json(path)
    except (ValueError, OSError) as exc:
        log.warning("Could not read %s (%s); starting fresh", path, exc)
        return _empty_doc()
    if not isinstance(doc, dict) or "clips" not in doc:
        log.warning("%s is not a clips document; starting fresh", path)
        return _empty_doc()
    doc.setdefault("version", SCHEMA_VERSION)
    doc.setdefault("next_id", len(doc.get("clips") or []) + 1)
    doc.setdefault("source", {})
    return doc


def save(ws: Workspace, doc: Dict[str, Any]) -> Any:
    doc["updated_at"] = time.time()
    return ws.write_json(ws.clips_path, doc)


def _untouched(clip: Dict[str, Any]) -> bool:
    """Has the human invested anything in this clip?

    Only untouched clips are safe to drop when re-analysis no longer proposes
    them. The original check looked at status and `output` alone, which silently
    deleted a clip the user had rated, retitled, tagged or given reel settings -
    real work, thrown away on the next page load.
    """
    return (
        clip.get("status", STATUS_PENDING) == STATUS_PENDING
        and not clip.get("output")
        and not clip.get("reel")
        and not clip.get("reel_output")
        and not clip.get("rating")
        and not clip.get("title")
        and not clip.get("tags")
        and not clip.get("notes")
    )


def _overlap_iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Intersection over union of two time ranges."""
    overlap = min(a_end, b_end) - max(a_start, b_start)
    if overlap <= 0:
        return 0.0
    union = max(a_end, b_end) - min(a_start, b_start)
    return overlap / union if union > 0 else 0.0


def _new_clip(candidate: Dict[str, Any], clip_id: str) -> Dict[str, Any]:
    now = time.time()
    start = float(candidate["start_time"])
    end = float(candidate["end_time"])
    return {
        "id": clip_id,
        "origin": "analyze",
        "source_start": start,
        "source_end": end,
        "start": start,
        "end": end,
        "status": STATUS_PENDING,
        "rating": 0,
        "title": "",
        "description": str(candidate.get("description") or ""),
        "why": str(candidate.get("why") or ""),
        "tags": [],
        "notes": "",
        "stale": False,
        "orphaned": False,
        "output": None,
        "created_at": now,
        "updated_at": now,
    }


def reconcile(ws: Workspace, candidates_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a fresh candidates.json into existing review state.

    Matches new candidates to existing clips by time overlap so that approvals,
    ratings and hand-edited in/out points survive a rubric change. Returns the
    saved document.
    """
    doc = load(ws)
    existing: List[Dict[str, Any]] = list(doc.get("clips") or [])
    incoming: List[Dict[str, Any]] = list(candidates_doc.get("clips") or [])

    # Score every (incoming, existing) pair, then take them greedily by best
    # overlap so one candidate can't claim a clip a closer one wanted.
    pairs = []
    for i, cand in enumerate(incoming):
        try:
            c_start = float(cand["start_time"])
            c_end = float(cand["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        for j, clip in enumerate(existing):
            iou = _overlap_iou(
                c_start,
                c_end,
                float(clip.get("source_start", clip.get("start", 0.0))),
                float(clip.get("source_end", clip.get("end", 0.0))),
            )
            if iou >= MATCH_IOU_THRESHOLD:
                pairs.append((iou, i, j))
    pairs.sort(reverse=True)

    matched_incoming = {}
    matched_existing = set()
    for iou, i, j in pairs:
        if i in matched_incoming or j in matched_existing:
            continue
        matched_incoming[i] = j
        matched_existing.add(j)

    next_id = int(doc.get("next_id", 1))
    result: List[Dict[str, Any]] = []
    now = time.time()

    for i, cand in enumerate(incoming):
        if i in matched_incoming:
            clip = dict(existing[matched_incoming[i]])
            new_start = float(cand["start_time"])
            new_end = float(cand["end_time"])
            drift = max(
                abs(new_start - float(clip.get("source_start", new_start))),
                abs(new_end - float(clip.get("source_end", new_end))),
            )
            # Refresh what the model said; keep everything the human decided.
            clip["description"] = str(cand.get("description") or "")
            clip["why"] = str(cand.get("why") or "")
            clip["source_start"] = new_start
            clip["source_end"] = new_end
            clip["stale"] = drift > STALE_DRIFT_SECONDS
            clip["orphaned"] = False
            clip["updated_at"] = now
            result.append(clip)
        else:
            clip_id = "c_{0:04d}".format(next_id)
            next_id += 1
            result.append(_new_clip(cand, clip_id))

    # Existing clips the new analysis didn't produce. Keep anything the user
    # touched (they may already have cut it); drop untouched pending ones.
    dropped = 0
    for j, clip in enumerate(existing):
        if j in matched_existing:
            continue
        if _untouched(clip):
            dropped += 1
            continue
        clip = dict(clip)
        clip["orphaned"] = True
        clip["updated_at"] = now
        result.append(clip)

    result.sort(key=lambda c: float(c.get("start", 0.0)))

    doc["clips"] = result
    doc["next_id"] = next_id
    doc["source"] = {
        "rubric_sha1": candidates_doc.get("rubric_sha1"),
        "model": candidates_doc.get("model"),
        "candidates_sha1": hashlib.sha1(
            repr(
                [
                    (c.get("start_time"), c.get("end_time"))
                    for c in incoming
                ]
            ).encode("utf-8")
        ).hexdigest(),
    }

    kept = len(matched_incoming)
    added = len(incoming) - kept
    orphans = sum(1 for c in result if c.get("orphaned"))
    log.info(
        "Review state: %d matched, %d new, %d orphaned, %d dropped",
        kept,
        added,
        orphans,
        dropped,
    )
    save(ws, doc)
    return doc


def ensure_imported(ws: Workspace) -> Dict[str, Any]:
    """Make sure clips.json reflects the current candidates.json."""
    if not ws.candidates_path.exists():
        return load(ws)
    candidates = ws.read_json(ws.candidates_path)
    return reconcile(ws, candidates)


def get_clip(doc: Dict[str, Any], clip_id: str) -> Optional[Dict[str, Any]]:
    for clip in doc.get("clips") or []:
        if clip.get("id") == clip_id:
            return clip
    return None


def update_clip(
    ws: Workspace,
    clip_id: str,
    changes: Dict[str, Any],
    duration: Optional[float] = None,
) -> Dict[str, Any]:
    """Apply user edits to one clip. Returns the updated clip."""
    doc = load(ws)
    clip = get_clip(doc, clip_id)
    if clip is None:
        raise KeyError("No clip {0!r} in {1}".format(clip_id, ws.clips_path))

    for field, value in changes.items():
        if field not in EDITABLE_FIELDS:
            continue
        if field == "status":
            if value not in VALID_STATUSES:
                raise ValueError("Invalid status {0!r}".format(value))
            clip["status"] = value
        elif field in ("start", "end"):
            clip[field] = round(max(0.0, float(value)), 3)
        elif field == "rating":
            clip["rating"] = max(0, min(5, int(value)))
        elif field == "tags":
            clip["tags"] = [str(t) for t in (value or [])]
        elif field == "reel":
            # Needs its own branch: the fallback below stringifies, which would
            # turn the spec dict into "{'preset': ...}". normalize() raises
            # ValueError on bad input, which the API maps to a 400.
            from .reelspec import normalize as _normalize_reel

            clip["reel"] = None if value in (None, {}, "") else _normalize_reel(value)
        else:
            clip[field] = str(value or "")

    if clip["end"] <= clip["start"]:
        raise ValueError(
            "end ({0}) must be after start ({1})".format(clip["end"], clip["start"])
        )
    if duration and clip["end"] > duration:
        clip["end"] = round(float(duration), 3)

    clip["updated_at"] = time.time()
    doc["clips"] = sorted(
        doc.get("clips") or [], key=lambda c: float(c.get("start", 0.0))
    )
    save(ws, doc)
    return clip


def add_manual_clip(
    ws: Workspace,
    start: float,
    end: float,
    title: str = "",
    description: str = "",
) -> Dict[str, Any]:
    """Create a clip the model didn't propose (e.g. from a transcript selection)."""
    doc = load(ws)
    next_id = int(doc.get("next_id", 1))
    clip_id = "c_{0:04d}".format(next_id)
    now = time.time()
    clip = {
        "id": clip_id,
        "origin": "manual",
        "source_start": float(start),
        "source_end": float(end),
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "status": STATUS_APPROVED,
        "rating": 0,
        "title": str(title or ""),
        "description": str(description or ""),
        "why": "Added by hand.",
        "tags": [],
        "notes": "",
        "stale": False,
        "orphaned": False,
        "output": None,
        "created_at": now,
        "updated_at": now,
    }
    doc["clips"] = sorted(
        list(doc.get("clips") or []) + [clip], key=lambda c: float(c.get("start", 0.0))
    )
    doc["next_id"] = next_id + 1
    save(ws, doc)
    return clip


def clips_for_cutting(doc: Dict[str, Any], clip_ids=None) -> List[Dict[str, Any]]:
    """Which clips a cut run should process."""
    clips = list(doc.get("clips") or [])
    if clip_ids:
        wanted = set(clip_ids)
        return [c for c in clips if c.get("id") in wanted]
    return [
        c
        for c in clips
        if c.get("status") in (STATUS_APPROVED, STATUS_CUT, STATUS_FAILED)
    ]


def counts(doc: Dict[str, Any]) -> Dict[str, int]:
    result = {status: 0 for status in VALID_STATUSES}
    result["total"] = 0
    result["reeled"] = 0
    for clip in doc.get("clips") or []:
        result["total"] += 1
        status = clip.get("status", STATUS_PENDING)
        if status in result:
            result[status] += 1
        # A reel can be rendered straight from the VOD without the clip ever
        # being approved/cut (see stages/reel.py), so this is tracked
        # separately from status rather than folded into "cut".
        if (clip.get("reel_output") or {}).get("file"):
            result["reeled"] += 1
    return result
