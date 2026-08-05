"""Manifest generation: what got cut, and why.

This is the hand-off to editing - a folder of clips plus a listing of each one's
description and the reason it was picked. Written as both JSON (for tooling) and
CSV (for reading in a spreadsheet).

The CSV is deliberately written as utf-8-sig. Excel on Windows assumes the
system codepage without a BOM, which turns Devanagari descriptions into
mojibake; the BOM makes it open correctly on a double-click.
"""

import csv
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import review
from .config import Settings
from .utils import format_timestamp, get_logger
from .workspace import Workspace

log = get_logger(__name__)

CSV_COLUMNS = [
    "id",
    "file",
    "start",
    "end",
    "duration",
    "start_hms",
    "end_hms",
    "title",
    "description",
    "why",
    "tags",
    "rating",
    "status",
    "bytes",
    "re_encode",
]


def _clip_rows(ws: Workspace, settings: Settings) -> List[Dict[str, Any]]:
    if not ws.clips_path.exists():
        return []
    doc = review.load(ws)
    rows = []
    for clip in doc.get("clips") or []:
        output = clip.get("output") or {}
        # Only clips that actually produced a file belong in a manifest.
        if not output.get("file"):
            continue
        start = float(clip.get("start", 0.0))
        end = float(clip.get("end", 0.0))
        rows.append(
            {
                "id": clip.get("id"),
                "file": output.get("file"),
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": output.get("duration", round(end - start, 3)),
                "start_hms": format_timestamp(start),
                "end_hms": format_timestamp(end),
                "title": clip.get("title") or "",
                "description": clip.get("description") or "",
                "why": clip.get("why") or "",
                "tags": ", ".join(clip.get("tags") or []),
                "rating": clip.get("rating", 0),
                "status": clip.get("status"),
                "bytes": output.get("bytes"),
                "re_encode": bool(output.get("re_encode")),
            }
        )
    return rows


def write_manifest(ws: Workspace, settings: Settings) -> Tuple[Path, Path]:
    """Write manifest.json and manifest.csv. Returns both paths."""
    state = ws.read_state()
    rows = _clip_rows(ws, settings)

    candidates_meta = {}
    if ws.candidates_path.exists():
        try:
            candidates = ws.read_json(ws.candidates_path)
            candidates_meta = {
                "model": candidates.get("model"),
                "rubric_sha1": candidates.get("rubric_sha1"),
            }
        except (ValueError, OSError):
            pass

    payload = {
        "version": 1,
        "generated_at": time.time(),
        "slug": ws.slug,
        "url": state.get("url"),
        "title": state.get("title"),
        "uploader": state.get("uploader"),
        "upload_date": state.get("upload_date"),
        "source_duration": state.get("duration"),
        "clip_count": len(rows),
        "clips": rows,
    }
    payload.update(candidates_meta)

    ws.write_json(ws.manifest_json_path, payload)

    # newline="" per the csv module docs, or Windows gets blank rows between
    # every record.
    with ws.manifest_csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    log.info(
        "Wrote manifest with %d clip(s): %s, %s",
        len(rows),
        ws.manifest_json_path.name,
        ws.manifest_csv_path.name,
    )
    return ws.manifest_json_path, ws.manifest_csv_path
