"""Background job runner and the SSE event bus.

Stages block - transcription for ~90 minutes - so they run on worker threads
while the event loop stays free. Threads rather than subprocesses because the
stage functions already have the right shape, none of the work is Python-bound
(CTranslate2 releases the GIL, ffmpeg/yt-dlp are subprocesses, Claude is
network I/O), and the 3 GB Whisper model stays loaded between runs.

Two lanes so a three-second clip cut never queues behind a 90-minute transcribe.
"""

import json
import queue
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from ..config import Settings, load_settings
from ..progress import JobCancelled, Progress
from ..utils import StageError, ToolMissingError, get_logger
from ..workspace import Workspace

log = get_logger(__name__)

# "reel" is heavy: a batch x264 render runs for minutes, and the light lane's
# whole point is that a three-second clip cut never queues behind long work.
# "diarize" is heavy for the same reason as transcribe - CPU-bound (no CUDA
# on this machine) and can run for a long time on a multi-hour VOD.
HEAVY_KINDS = ("download", "audio", "transcribe", "analyze", "diarize", "pipeline", "reel")
# "chat" is light: it's network-bound (a 4.6h stream was 15 requests in 16s) and
# must not queue behind an x264 render, because chat expires with the VOD.
# "transliterate" is the same shape as chat: a handful of batched Claude API
# calls, not CPU/GPU-bound, and shouldn't queue behind a long transcribe/reel.
LIGHT_KINDS = ("cut", "chat", "transliterate", "manifest", "cleanup", "waveform", "benchmark")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

TERMINAL = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED)


class Job(object):
    def __init__(self, kind: str, slug: str, options: Optional[Dict[str, Any]] = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.slug = slug
        self.options = options or {}
        self.status = STATUS_QUEUED
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.fraction = None
        self.phase = None
        self.label = None
        self.eta_seconds = None
        self.error = None
        self.result = None
        self.cancel_event = threading.Event()
        self.log_tail = deque(maxlen=500)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "slug": self.slug,
            "options": self.options,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "fraction": self.fraction,
            "phase": self.phase,
            "label": self.label,
            "eta_seconds": self.eta_seconds,
            "error": self.error,
            "result": self.result,
        }


class EventBus(object):
    """Fan-out to connected SSE clients.

    Worker threads publish; the asyncio loop consumes. Every event gets an id so
    a reconnecting browser can replay what it missed via Last-Event-ID.
    """

    def __init__(self, history=400):
        self._lock = threading.Lock()
        self._subscribers = []  # List[asyncio.Queue]
        self._loop = None
        self._next_id = 1
        self._history = deque(maxlen=history)

    def bind_loop(self, loop):
        self._loop = loop

    def subscribe(self, q):
        with self._lock:
            self._subscribers.append(q)

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def replay_since(self, last_id: Optional[int]) -> List[Dict[str, Any]]:
        if not last_id:
            return []
        with self._lock:
            return [e for e in self._history if e["id"] > last_id]

    def publish(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            event = {"id": self._next_id, "kind": kind, "payload": payload}
            self._next_id += 1
            self._history.append(event)
            targets = list(self._subscribers)
            loop = self._loop

        if loop is None:
            return
        for q in targets:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                # Loop is shutting down.
                pass


class JobRunner(object):
    def __init__(self, settings: Settings, bus: EventBus):
        self.settings = settings
        self.bus = bus
        self.jobs = {}  # type: Dict[str, Job]
        self._order = deque(maxlen=200)
        self._lock = threading.Lock()
        self._queues = {
            "heavy": queue.Queue(),
            "light": queue.Queue(),
        }
        self._threads = []
        self._stopping = threading.Event()
        self._handlers = {}  # type: Dict[str, Callable]

    def register(self, kind: str, handler: Callable) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        for lane in ("heavy", "light"):
            t = threading.Thread(
                target=self._worker, args=(lane,), name="clipbot-{0}".format(lane)
            )
            t.daemon = True
            t.start()
            self._threads.append(t)
        log.info("Job runner started (2 lanes)")

    def stop(self) -> None:
        self._stopping.set()
        for lane in self._queues:
            self._queues[lane].put(None)

    # ---- submission -----------------------------------------------------

    def submit(self, kind: str, slug: str, options=None) -> Job:
        if kind not in self._handlers:
            raise StageError("Unknown job kind {0!r}".format(kind))

        job = Job(kind, slug, options)
        with self._lock:
            # One job per workspace at a time keeps state.json writes sane and
            # stops two transcribes fighting over the CPU.
            for other in self.jobs.values():
                if (
                    other.slug == slug
                    and other.status in (STATUS_QUEUED, STATUS_RUNNING)
                ):
                    raise StageError(
                        "{0} is already {1} on this workspace".format(
                            other.kind, other.status
                        )
                    )
            self.jobs[job.id] = job
            self._order.append(job.id)

        lane = "heavy" if kind in HEAVY_KINDS else "light"
        self._queues[lane].put(job.id)
        self.bus.publish("job", job.to_dict())
        log.info("Queued %s for %s (%s)", kind, slug, job.id)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status in TERMINAL:
            return False
        job.cancel_event.set()
        if job.status == STATUS_QUEUED:
            self._finish(job, STATUS_CANCELLED)
        return True

    def recent(self, limit=40) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(self._order)[-limit:]
            return [self.jobs[i].to_dict() for i in reversed(ids) if i in self.jobs]

    def active_for(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for job in self.jobs.values():
                if job.slug == slug and job.status in (STATUS_QUEUED, STATUS_RUNNING):
                    return job.to_dict()
        return None

    # ---- execution ------------------------------------------------------

    def _worker(self, lane: str) -> None:
        q = self._queues[lane]
        while not self._stopping.is_set():
            job_id = q.get()
            if job_id is None:
                break
            job = self.jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                continue
            self._run(job)

    def _run(self, job: Job) -> None:
        job.status = STATUS_RUNNING
        job.started_at = time.time()
        self.bus.publish("job", job.to_dict())

        set_current_job(job)
        try:
            ws = Workspace.open(self.settings.work_root / job.slug)
            progress = Progress(
                sink=lambda kind, payload: self._on_progress(job, kind, payload),
                cancel_event=job.cancel_event,
            )
            handler = self._handlers[job.kind]
            result = handler(ws, self.settings, job, progress)
            job.result = str(result) if result is not None else None
            self._finish(job, STATUS_SUCCEEDED)
        except JobCancelled:
            log.info("Job %s cancelled", job.id)
            self._finish(job, STATUS_CANCELLED)
        except (StageError, ToolMissingError, FileNotFoundError) as exc:
            job.error = str(exc)
            log.error("Job %s failed: %s", job.id, exc)
            self._finish(job, STATUS_FAILED)
        except Exception as exc:  # unexpected - keep the server alive
            job.error = "{0}: {1}".format(type(exc).__name__, exc)
            log.exception("Job %s crashed", job.id)
            self._finish(job, STATUS_FAILED)
        finally:
            set_current_job(None)

    def _on_progress(self, job: Job, kind: str, payload: Dict[str, Any]) -> None:
        if kind == "progress":
            job.fraction = payload.get("fraction")
            job.phase = payload.get("phase")
            job.label = payload.get("label")
            job.eta_seconds = payload.get("eta_seconds")
            self.bus.publish(
                "progress",
                {
                    "job_id": job.id,
                    "slug": job.slug,
                    "fraction": job.fraction,
                    "phase": job.phase,
                    "label": job.label,
                    "eta_seconds": job.eta_seconds,
                },
            )
        elif kind == "phase":
            job.phase = payload.get("phase")
            self.bus.publish(
                "progress",
                {"job_id": job.id, "slug": job.slug, "phase": job.phase, "fraction": None},
            )
        else:
            self.bus.publish(kind, dict(payload, job_id=job.id, slug=job.slug))

    def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.finished_at = time.time()
        if status == STATUS_SUCCEEDED:
            job.fraction = 1.0
        self.bus.publish("job", job.to_dict())
        self.bus.publish("workspace", {"slug": job.slug})


# --- log bridge ----------------------------------------------------------

_CURRENT_JOB = threading.local()


def set_current_job(job) -> None:
    _CURRENT_JOB.job = job


def get_current_job():
    return getattr(_CURRENT_JOB, "job", None)


class BusLogHandler(object):
    """Forwards clipbot log records to the event bus, tagged with the job.

    Implemented as a duck-typed handler rather than subclassing at import time
    so the logging import stays local to the server.
    """

    def __init__(self, bus: EventBus):
        import logging

        self.bus = bus

        class _Handler(logging.Handler):
            def emit(inner, record):
                try:
                    job = get_current_job()
                    payload = {
                        "message": record.getMessage(),
                        "level": record.levelname,
                        "logger": record.name,
                        "time": record.created,
                        "job_id": job.id if job else None,
                        "slug": job.slug if job else None,
                    }
                    if job is not None:
                        job.log_tail.append(payload)
                    bus.publish("log", payload)
                except Exception:
                    pass

        self._handler = _Handler()

    def install(self, level=None) -> None:
        import logging

        root = logging.getLogger("clipbot")
        self._handler.setLevel(level or logging.INFO)
        root.addHandler(self._handler)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
