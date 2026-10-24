"""Stage 2d: the reaction map - tag every second of a stream's audio (laughter, shouting, gunfire, music...).

`scan_events(ws, settings, force=False, first=None, last=None, progress=NULL_PROGRESS) -> Path` reads
`audio.wav`, tags it with the PANNs `Cnn14_16k` model (`panns.py`) and writes `events.json` (`audioevents.py`
builds the document). Opt-in, never part of `run`: it needs PyTorch and a 359 MB checkpoint that only an explicit
`clipbot events fetch-model` downloads.

Everything the editor does with the result is a *hint*, never a fact: the model was trained on ten-second YouTube
clips, not Hinglish commentary over a game's mix. The measurements that decide how far to trust it are in
`docs/edit-enhancements-plan.md` section 0.8; read them before leaning on a number from `events.json`.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .. import audioevents, panns
from ..config import Settings
from ..progress import NULL_PROGRESS, Progress
from ..utils import StageError, get_logger
from ..workspace import Workspace

log = get_logger(__name__)

STAGE = "events"


def events_path(ws: Workspace) -> Path:
    return ws.root / "events.json"


def load(ws: Workspace) -> Optional[Dict[str, Any]]:
    p = events_path(ws)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _set_threads(settings: Settings) -> None:
    n = int(settings.get("events.threads", 0) or 0)
    if n > 0:
        panns._torch().set_num_threads(n)


def scan_events(ws: Workspace, settings: Settings, force: bool = False, first: Optional[int] = None,
                last: Optional[int] = None, progress: Progress = NULL_PROGRESS) -> Path:
    """Tag `audio.wav` and write `events.json`. `first`/`last` (stream seconds) scan only that stretch - for trying
    the model on a few minutes before committing hours of CPU to a whole stream; the document says where it starts."""
    out = events_path(ws)
    if out.exists() and not force:
        log.info("%s already scanned (use force to redo)", out.name)
        return out
    if not ws.audio_path.exists():
        raise StageError("{0} has no audio.wav; run the audio stage first".format(ws.slug))
    model = panns.load_model(panns.checkpoint_path(settings))
    _set_threads(settings)
    duration = panns.wav_seconds(ws.audio_path)
    a = int(first or 0)
    b = int(last) if last is not None else int(duration)
    classes = audioevents.union_classes()
    progress.phase("tag", total=max(1, b - a), unit="seconds")
    t0 = time.time()

    def tick(done: int, total: int) -> None:
        progress.check_cancelled()
        progress.update(done, label="{0:.0f} min".format((a + done) / 60.0))

    probs = panns.tag_second_grid(model, ws.audio_path, duration, first=a, last=b, classes=classes, progress=tick,
                                  cancelled=lambda: progress.cancelled)
    series = audioevents.category_series(probs, classes)
    took = time.time() - t0
    doc = audioevents.build_document(ws.slug, duration, series, first=a, extra={
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "scan_seconds": round(took, 1),
        "realtime_factor": round((b - a) / took, 1) if took > 0 else None})
    ws.write_json(out, doc)
    ws.mark_stage(STAGE, seconds=doc["seconds"], peaks={k: len(v) for k, v in doc["peaks"].items()})
    log.info("Events: %d s tagged in %.0f s (%.0fx realtime); peaks %s", doc["seconds"], took, doc["realtime_factor"] or 0,
             {k: len(v) for k, v in doc["peaks"].items() if k in ("laughter", "shout", "gunfire")})
    return out
