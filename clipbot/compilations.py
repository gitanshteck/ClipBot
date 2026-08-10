"""Named multi-range compilations, stored in a sidecar `compilations.json`.

A compilation is an ordered list of source VOD ranges that stages/compile.py
cuts and concatenates into a single output video - the mechanism behind
`clipbot compile`. Deliberately not built on clips.json/review.py: a
compilation's segments are not individually-cuttable clips a human
approves/rejects, they're fragments of one assembled video, so mixing them
into the approve/reject/cut review queue would make a normal `clipbot cut`
run try to cut each one out separately too.

`compilations.json` shape:

    {
      "version": 1,
      "updated_at": 1785770827.5,
      "next_id": 2,
      "compilations": [
        {
          "id": "comp_0001",
          "name": "catan-highlights",
          "segments": [
            {"start": 15009.1, "end": 15043.3, "label": "..."},
            ...
          ],
          "output": {"file": "clips/compilations/catan-highlights.mp4",
                     "bytes": 12345, "duration": 421.2,
                     "rendered_at": 1785770900.1, "fingerprint": "..."} | null,
          "created_at": ..., "updated_at": ...
        }
      ]
    }
"""

import shutil
import time
from typing import Any, Dict, List, Optional

from .config import Settings
from .utils import get_logger
from .workspace import Workspace

log = get_logger(__name__)

SCHEMA_VERSION = 1


def _empty_doc() -> Dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": time.time(),
        "next_id": 1,
        "compilations": [],
    }


def load(ws: Workspace) -> Dict[str, Any]:
    """Read compilations.json, or an empty document if it doesn't exist yet."""
    path = ws.compilations_path
    if not path.exists():
        return _empty_doc()
    try:
        doc = ws.read_json(path)
    except (ValueError, OSError) as exc:
        log.warning("Could not read %s (%s); starting fresh", path, exc)
        return _empty_doc()
    if not isinstance(doc, dict) or "compilations" not in doc:
        log.warning("%s is not a compilations document; starting fresh", path)
        return _empty_doc()
    doc.setdefault("version", SCHEMA_VERSION)
    doc.setdefault("next_id", len(doc.get("compilations") or []) + 1)
    return doc


def save(ws: Workspace, doc: Dict[str, Any]) -> Any:
    doc["updated_at"] = time.time()
    return ws.write_json(ws.compilations_path, doc)


def get(doc: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for comp in doc.get("compilations") or []:
        if comp.get("name") == name:
            return comp
    return None


def _validate_name(name: Any) -> str:
    """Reject a name that could escape the workspace when used as a
    filesystem path segment.

    Mirrors the read-side check `server/app.py`'s `media_compilation` route
    already applies - this closes the write-side gap, since a compilation's
    `name` is used directly to build `render_compilation`'s output path and
    `delete`'s unlink/rmtree targets (`stages/compile.py`,
    `Workspace.compile_scratch_dir`).
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("A compilation name is required.")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(
            "Compilation name {0!r} may not contain '/', '\\', or '..'.".format(name)
        )
    return name


def _normalize_segments(
    segments: List[Dict[str, Any]], min_duration: float = 0.0
) -> List[Dict[str, Any]]:
    normalized = []
    for seg in segments:
        start = float(seg["start"])
        end = float(seg["end"])
        if end <= start:
            raise ValueError(
                "segment end ({0}) must be after start ({1})".format(end, start)
            )
        if end - start < min_duration:
            raise ValueError(
                "segment {0:.2f}-{1:.2f} ({2:.2f}s) is shorter than "
                "compile.min_duration ({3}s)".format(start, end, end - start, min_duration)
            )
        normalized.append(
            {"start": start, "end": end, "label": str(seg.get("label") or "")}
        )
    normalized.sort(key=lambda s: s["start"])
    for prev, cur in zip(normalized, normalized[1:]):
        if cur["start"] < prev["end"]:
            raise ValueError(
                "segments {0:.2f}-{1:.2f} and {2:.2f}-{3:.2f} overlap".format(
                    prev["start"], prev["end"], cur["start"], cur["end"]
                )
            )
    return normalized


def upsert(
    ws: Workspace, name: str, segments: List[Dict[str, Any]], settings: Settings
) -> Dict[str, Any]:
    """Create a compilation, or replace an existing one's segment list.

    Replacing (rather than merging) is deliberate: a compilation is authored
    as a whole ordered list, not built up incrementally like clips.json's
    per-clip approvals.
    """
    name = _validate_name(name)
    if not segments:
        raise ValueError("A compilation needs at least one segment.")

    doc = load(ws)
    min_duration = float(settings.get("compile.min_duration", 0.5))
    normalized = _normalize_segments(segments, min_duration)
    now = time.time()

    existing = get(doc, name)
    if existing is not None:
        existing["segments"] = normalized
        existing["updated_at"] = now
        save(ws, doc)
        return existing

    next_id = int(doc.get("next_id", 1))
    comp = {
        "id": "comp_{0:04d}".format(next_id),
        "name": name,
        "segments": normalized,
        "output": None,
        "created_at": now,
        "updated_at": now,
    }
    doc["compilations"] = list(doc.get("compilations") or []) + [comp]
    doc["next_id"] = next_id + 1
    save(ws, doc)
    return comp


def set_output(ws: Workspace, name: str, output: Dict[str, Any]) -> Dict[str, Any]:
    """Record a completed render's output metadata on its compilation."""
    doc = load(ws)
    comp = get(doc, name)
    if comp is None:
        raise KeyError("No compilation named {0!r}".format(name))
    comp["output"] = output
    comp["updated_at"] = time.time()
    save(ws, doc)
    return comp


def add_segment(
    ws: Workspace, name: str, segment: Dict[str, Any], settings: Settings
) -> Dict[str, Any]:
    """Append one segment to a compilation, creating it if it doesn't exist.

    The additive counterpart to `upsert` (whole-list replace, meant for the
    compile page's own editor save). This is the one entry point two browser
    tabs can legitimately hit concurrently for the same compilation - e.g.
    two clips added to the same named compilation from two review.html tabs
    in quick succession - so it's the one write in this module guarded by
    `ws.lock`, unlike the rest, which follow review.py/speakers.py's existing
    unlocked-JSON-sidecar convention.
    """
    name = _validate_name(name)
    min_duration = float(settings.get("compile.min_duration", 0.5))
    with ws.lock:
        doc = load(ws)
        comp = get(doc, name)
        seg = _normalize_segments([segment], min_duration)[0]
        now = time.time()

        if comp is None:
            next_id = int(doc.get("next_id", 1))
            comp = {
                "id": "comp_{0:04d}".format(next_id),
                "name": name,
                "segments": [],
                "output": None,
                "created_at": now,
                "updated_at": now,
            }
            doc["compilations"] = list(doc.get("compilations") or []) + [comp]
            doc["next_id"] = next_id + 1

        comp["segments"] = _normalize_segments(comp["segments"] + [seg], min_duration)
        comp["updated_at"] = now
        save(ws, doc)
        return comp


def delete(ws: Workspace, name: str, purge: bool = True) -> None:
    """Remove a named compilation. Raises KeyError if it doesn't exist.

    `purge` (the dashboard's default) also deletes the rendered .mp4 and the
    per-compilation scratch directory of cut segments - a dashboard delete is
    a real delete, not just dropping the JSON entry. Both removals are
    best-effort: a file that's already gone (never rendered, or removed by
    hand) is not an error.
    """
    name = _validate_name(name)
    doc = load(ws)
    comps = doc.get("compilations") or []
    remaining = [c for c in comps if c.get("name") != name]
    if len(remaining) == len(comps):
        raise KeyError(name)
    doc["compilations"] = remaining
    save(ws, doc)

    if purge:
        mp4 = ws.compilations_dir / "{0}.mp4".format(name)
        if mp4.exists():
            mp4.unlink()
        shutil.rmtree(ws.compile_scratch_dir(name), ignore_errors=True)


def list_compilations(ws: Workspace) -> List[Dict[str, Any]]:
    return list(load(ws).get("compilations") or [])
