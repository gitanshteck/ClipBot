"""Shared helpers: logging, subprocess execution, tool discovery."""

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class ToolMissingError(RuntimeError):
    """An external binary the stage depends on could not be found."""


class StageError(RuntimeError):
    """A pipeline stage failed in an expected, reportable way."""


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # These libraries log every HTTP request at INFO, which buries our own
    # progress output during model downloads.
    for noisy in ("urllib3", "httpx", "httpcore", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def resolve_tool(command: str, hint: str = "") -> str:
    """Return an executable path for `command`, or raise with an install hint.

    Tolerates a value that can't be a path at all: a `CLIPBOT_YT_DLP` (or any
    tool) set with stray quotes or a control character makes `Path.is_file()`
    raise `OSError` on Windows (WinError 123), which used to surface as an
    unhandled traceback - or a 500 from the dashboard - instead of the
    "not found" message with its install hint. Wrapping quotes, which a shell
    or `setx` easily leaves in an environment variable, are stripped.
    """
    command = str(command).strip()
    if len(command) >= 2 and command[0] == command[-1] == '"':
        command = command[1:-1]
    try:
        if Path(command).is_file():
            return str(Path(command))
    except (OSError, ValueError):
        pass

    try:
        found = shutil.which(command)
    except (OSError, ValueError):
        found = None
    if found:
        return found

    message = (
        "Required tool '{0}' was not found on PATH.\n"
        "Either install it and reopen your shell, or set an absolute path in "
        "config/settings.json under \"tools\"."
    ).format(command)
    if hint:
        message += "\n" + hint
    raise ToolMissingError(message)


def run_command(
    argv: Sequence[str],
    *,
    log: Optional[logging.Logger] = None,
    capture: bool = False,
    check: bool = True,
    tee: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess, streaming its output unless `capture` is set.

    `tee` captures the output *and* forwards each line to the logger as it
    arrives - needed for long downloads, where you want live progress but also
    want the actual error text if the command fails.
    """
    log = log or get_logger(__name__)
    log.debug("exec: %s", " ".join(str(a) for a in argv))

    if tee:
        proc = subprocess.Popen(
            [str(a) for a in argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        lines = []
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            lines.append(line)
            log.debug("%s", line)
        proc.wait()
        result = subprocess.CompletedProcess(
            argv, proc.returncode, "\n".join(lines), "\n".join(lines)
        )
    else:
        result = subprocess.run(
            [str(a) for a in argv],
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    if check and result.returncode != 0:
        detail = ""
        if capture and result.stderr:
            detail = "\n" + result.stderr.strip()[-4000:]
        raise StageError(
            "Command failed (exit {0}): {1}{2}".format(
                result.returncode, " ".join(str(a) for a in argv), detail
            )
        )
    return result


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(value: str, max_length: int = 60) -> str:
    slug = _SLUG_RE.sub("-", value).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:max_length].strip("-.") or "vod").lower()


def format_timestamp(seconds: float) -> str:
    """Seconds -> HH:MM:SS.mmm, the form ffmpeg accepts for -ss/-to."""
    if seconds < 0:
        seconds = 0.0
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:  # rounding spilled over
        millis = 0
        secs += 1
    return "{0:02d}:{1:02d}:{2:02d}.{3:03d}".format(hours, minutes, secs, millis)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "{0:.1f} {1}".format(size, unit)
        size /= 1024
    return "{0:.1f} TB".format(size)


def find_largest_file(directory: Path, extensions: Sequence[str]) -> Optional[Path]:
    """Largest file in `directory` matching any of `extensions` (e.g. ['.mp4'])."""
    candidates: List[Path] = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in {e.lower() for e in extensions}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)
