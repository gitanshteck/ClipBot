"""Per-workspace speaker assignment, stored in a sidecar `speaker_map.json`.

Mirrors `review.py`'s sidecar-over-immutable-artifact pattern:
`stages/transcribe.py` stays the sole writer of `transcript.json` and
`stages/diarize.py` stays the sole writer of `diarization.json` - this module
only ever reads those two and writes the assignment overlay on top, the same
single-writer-per-file discipline as `candidates.json`/`clips.json`.

speaker_map.json:
    {
      "version": 1,
      "updated_at": 1785940944.4,
      "assignments": {"0": "SPEAKER_00", "1": "SPEAKER_00", "7": "host"}
    }

Keys are transcript segment ids (JSON object keys are always strings, so an
int id round-trips as its string form). Values are either a raw diarization
cluster label (before naming) or a `library.py` `speakers.json` id (after the
user names or merges a cluster via `library.save_speaker`) - this module
doesn't care which; it's the dashboard's "name this cluster" action that
rewrites matching values from one to the other in bulk.
"""

import time
from typing import Any, Dict, List, Optional

from .workspace import Workspace

SCHEMA_VERSION = 1

# Segments belonging to the same speaker with a gap shorter than this are
# treated as one continuous speaking span; a longer gap starts a new one, so
# an avatar overlay doesn't stay lit through a multi-second pause.
DEFAULT_MAX_GAP_SECONDS = 2.0


def _empty_doc() -> Dict[str, Any]:
    return {"version": SCHEMA_VERSION, "updated_at": time.time(), "assignments": {}}


def load(ws: Workspace) -> Dict[str, Any]:
    path = ws.speaker_map_path
    if not path.exists():
        return _empty_doc()
    try:
        doc = ws.read_json(path)
    except (ValueError, OSError):
        return _empty_doc()
    if not isinstance(doc, dict) or "assignments" not in doc:
        return _empty_doc()
    doc.setdefault("version", SCHEMA_VERSION)
    doc.setdefault("assignments", {})
    return doc


def save(ws: Workspace, doc: Dict[str, Any]) -> Any:
    doc["updated_at"] = time.time()
    return ws.write_json(ws.speaker_map_path, doc)


def assign_range(
    ws: Workspace, start_id: int, end_id: int, speaker_id: Optional[str]
) -> Dict[str, Any]:
    """Assign (or, with `speaker_id=None`, clear) every segment id in
    [start_id, end_id] inclusive. This is both the manual-override path and
    the only path at all for a workspace where diarization never ran."""
    doc = load(ws)
    assignments = doc.setdefault("assignments", {})
    for seg_id in range(int(start_id), int(end_id) + 1):
        key = str(seg_id)
        if speaker_id is None:
            assignments.pop(key, None)
        else:
            assignments[key] = str(speaker_id)
    save(ws, doc)
    return doc


def rename_speaker(ws: Workspace, old_id: str, new_id: str) -> Dict[str, Any]:
    """Bulk-rewrite every assignment pointing at `old_id` to `new_id` - the
    "name this diarization cluster" / "merge into an existing speaker"
    action, so naming a cluster is one call instead of one PATCH per segment.
    """
    doc = load(ws)
    assignments = doc.get("assignments") or {}
    for key, value in list(assignments.items()):
        if value == old_id:
            assignments[key] = new_id
    save(ws, doc)
    return doc


def bulk_from_diarization(
    ws: Workspace,
    transcript_doc: Dict[str, Any],
    diarization_doc: Dict[str, Any],
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Seed speaker_map.json from diarization.json's raw speaker turns.

    Each transcript segment is assigned the diarization turn with the
    largest time overlap. By default this only fills in segments that have
    no assignment yet - re-running diarization must not silently discard a
    human's manual corrections or already-named speakers; pass
    `overwrite=True` for the explicit "redo automatic assignment from
    scratch" action.
    """
    doc = load(ws)
    assignments = doc.setdefault("assignments", {})
    turns = sorted(
        (t for t in diarization_doc.get("turns") or []),
        key=lambda t: float(t.get("start", 0.0)),
    )

    for seg in transcript_doc.get("segments") or []:
        key = str(seg["id"])
        if not overwrite and key in assignments:
            continue
        seg_start, seg_end = float(seg["start"]), float(seg["end"])
        best_label, best_overlap = None, 0.0
        for turn in turns:
            t_start, t_end = float(turn.get("start", 0.0)), float(turn.get("end", 0.0))
            overlap = min(seg_end, t_end) - max(seg_start, t_start)
            if overlap > best_overlap:
                best_overlap, best_label = overlap, turn.get("speaker")
        if best_label:
            assignments[key] = str(best_label)

    save(ws, doc)
    return doc


def resolved_segments(
    transcript_doc: Dict[str, Any], map_doc: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Transcript segments merged with their assigned speaker (or None).

    The shared query function every downstream consumer uses - the
    dashboard's transcript view and `stages/reel.py`'s avatar overlay both
    read through this rather than each re-joining the two documents.
    """
    assignments = map_doc.get("assignments") or {}
    out = []
    for seg in transcript_doc.get("segments") or []:
        out.append({
            "id": seg["id"],
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": seg.get("text") or "",
            "speaker_id": assignments.get(str(seg["id"])),
        })
    return out


def speaking_spans(
    segments: List[Dict[str, Any]], max_gap: float = DEFAULT_MAX_GAP_SECONDS
) -> List[Dict[str, Any]]:
    """Collapse consecutive same-speaker segments into spans.

    Unassigned segments (`speaker_id is None`) contribute no span - the
    reel's avatar layer simply has nothing to show for that stretch, same as
    a clip with no chat messages has no chat layer. A gap longer than
    `max_gap` between two segments from the same speaker starts a new span,
    so an "appear" avatar doesn't stay lit through a multi-second pause.
    """
    spans: List[Dict[str, Any]] = []
    for seg in sorted(segments, key=lambda s: s["start"]):
        if not seg.get("speaker_id"):
            continue
        if (
            spans
            and spans[-1]["speaker_id"] == seg["speaker_id"]
            and seg["start"] - spans[-1]["end"] <= max_gap
        ):
            spans[-1]["end"] = max(spans[-1]["end"], seg["end"])
        else:
            spans.append({
                "speaker_id": seg["speaker_id"],
                "start": seg["start"],
                "end": seg["end"],
            })
    return spans
