"""Run yt-dlp with byte-progress reporting and cancellation.

Lifted out of `stages/download.py` (where it started life for Kick downloads)
so the YouTube stage can reuse it. Same reasoning `ffrun.py` gives for sharing
`run_ffmpeg`: a second copy would be a second place for the "cancel doesn't
actually kill the subprocess" bug to live.
"""

import collections
import subprocess
from pathlib import Path
from typing import Optional

from .progress import JobCancelled, Progress
from .utils import StageError, get_logger

log = get_logger(__name__)

# A private prefix on our --progress-template output, so the parser below
# can pick our machine-readable lines out of yt-dlp's normal stdout
# (destination/merge/postprocessor lines) by a simple startswith check
# rather than a regex over yt-dlp's human-readable bar (which varies in
# spacing/units between versions) - same reasoning ffrun.py gives for keying
# off ffmpeg's `-progress pipe:1` machine output instead of its normal log.
PROGRESS_MARKER = "CLIPBOT_PROGRESS "
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_MARKER +
    "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s"
)


def parse_progress_line(line: str):
    """Parse one `PROGRESS_MARKER`-prefixed line into (downloaded, total,
    speed) floats. Any field yt-dlp couldn't determine yet comes through as
    the literal string "NA" and becomes None. Returns None if the line
    doesn't have the expected field count (defensive against a future
    yt-dlp template-field change)."""
    parts = line[len(PROGRESS_MARKER):].split("|")
    if len(parts) != 4:
        return None

    def _f(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    downloaded, total, total_estimate, speed = (_f(p) for p in parts)
    return downloaded, (total or total_estimate), speed


def run_yt_dlp(argv, progress: Progress, log_path: Optional[Path] = None) -> None:
    """Run yt-dlp, parsing `--progress-template` output into `progress`
    updates and honoring cancellation.

    Mirrors `ffrun.run_ffmpeg`'s shape (the established pattern in this
    codebase for a long subprocess with parseable progress and a cancel
    button) rather than `utils.run_command`, which has neither.
    """
    proc = subprocess.Popen(
        [str(a) for a in argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail = collections.deque(maxlen=80)
    last_downloaded = 0.0
    phase_open = False
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # Matches run_command(tee=True)'s old behavior of debug-logging
            # every raw line - visible under `-v`, quiet by default, same as
            # every other long-running stage in this codebase (transcribe,
            # analyze, reel, compile all stay console-quiet during the work
            # itself and rely on a summary log line at the end).
            log.debug("%s", line)
            if progress.cancelled:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise JobCancelled("cancelled by request")

            if line.startswith(PROGRESS_MARKER):
                parsed = parse_progress_line(line)
                if parsed is None or parsed[0] is None:
                    continue
                downloaded, total, speed = parsed
                if not phase_open or downloaded < last_downloaded:
                    # `-f bv*+ba/b/best` downloads video then audio as two
                    # separate files; yt-dlp's own downloaded_bytes resets
                    # when the second one starts. A fresh phase here mirrors
                    # yt-dlp's own two-pass terminal output (video bar, then
                    # audio bar) - not a bug.
                    progress.phase("download", unit="bytes")
                    phase_open = True
                last_downloaded = downloaded
                progress.update(downloaded, total=total, rate=speed)
            else:
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
            "yt-dlp failed (exit {0}):\n{1}".format(
                proc.returncode, "\n".join(tail)[-4000:] or "(no output)"
            )
        )
