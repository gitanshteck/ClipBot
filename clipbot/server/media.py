"""Range-aware file serving, so the browser can seek in a 73-minute VOD.

Written by hand rather than relying on a particular Starlette version's
FileResponse range support - it's forty lines and removes a version dependency.
"""

import re
from pathlib import Path
from typing import Optional, Tuple

from starlette.responses import Response, StreamingResponse

CHUNK = 256 * 1024

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".json": "application/json",
    ".csv": "text/csv",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    # Library assets. Without a real type here the browser gets
    # application/octet-stream and <audio>/<img> refuse to play or draw it, so
    # auditioning a sound in the effects drawer silently does nothing.
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".apng": "image/apng",
    ".jpeg": "image/jpeg",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


def guess_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """Parse a single-range `Range` header. Browsers never send multi-ranges."""
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None
    raw_start, raw_end = match.group(1), match.group(2)
    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":
        # Suffix form: last N bytes.
        length = int(raw_end)
        if length <= 0:
            return None
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end


def _iter_file(path: Path, start: int, end: int):
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def serve_file(path: Path, range_header: str = "", download_name: str = ""):
    """Return a 200, 206 or 416 response for `path`."""
    size = path.stat().st_size
    media_type = guess_type(path)
    headers = {"accept-ranges": "bytes"}
    if download_name:
        headers["content-disposition"] = 'attachment; filename="{0}"'.format(
            download_name
        )

    span = parse_range(range_header, size)
    if span is None:
        if range_header and size:
            # A Range header we understood the syntax of but can't satisfy.
            match = _RANGE_RE.match(range_header.strip())
            if match:
                return Response(
                    status_code=416,
                    headers={"content-range": "bytes */{0}".format(size)},
                )
        headers["content-length"] = str(size)
        return StreamingResponse(
            _iter_file(path, 0, size - 1),
            media_type=media_type,
            headers=headers,
        )

    start, end = span
    headers["content-range"] = "bytes {0}-{1}/{2}".format(start, end, size)
    headers["content-length"] = str(end - start + 1)
    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )
