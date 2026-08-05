"""Run ffmpeg with progress reporting and cancellation.

Lifted verbatim out of `stages/reel.py`, where it started life, so the proxy
preview can reuse it. That reuse is the point: a preview that reimplemented
progress parsing and process termination would be a second place for the
"cancel doesn't actually kill ffmpeg" bug to live.

`utils.run_command` has neither progress nor cancellation, which is why a
multi-minute x264 encode cannot use it.
"""

import collections
import subprocess

from .progress import JobCancelled
from .utils import StageError, get_logger

log = get_logger(__name__)


def run_ffmpeg(argv, total_seconds, progress, base, span, log_path, out_path=None):
    """Run ffmpeg, reporting progress and honouring cancellation.

    `progress.update` is called with `base + fraction * span`, so a caller
    rendering many clips can map each one onto its own slice of an overall bar.
    `log_path` receives the command line plus the tail of ffmpeg's own output,
    which is what makes a failed render debuggable after the fact.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail = collections.deque(maxlen=80)
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if progress.cancelled:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise JobCancelled("cancelled by request")
            # -progress writes key=value on stdout. Note out_time_ms is really
            # microseconds - a long-standing ffmpeg quirk - so prefer out_time_us
            # and treat out_time_ms the same way.
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    micros = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if total_seconds:
                    frac = max(0.0, min(1.0, (micros / 1e6) / total_seconds))
                    progress.update(base + frac * span)
            elif "=" not in line:
                tail.append(line)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "\n".join([" ".join(str(a) for a in argv), ""] + list(tail)),
                encoding="utf-8",
            )
        except OSError:
            pass

    if proc.returncode != 0:
        raise StageError(
            "ffmpeg failed (exit {0}):\n{1}".format(
                proc.returncode, "\n".join(tail)[-4000:] or "(no output)"
            )
        )
