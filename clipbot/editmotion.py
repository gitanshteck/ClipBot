"""Motion graphics for the edit engine: Remotion compositions drawn to alpha video.

A `motion` overlay in an edit spec names a composition in `graphics/src/Root.jsx`
(`OpTitle`, `Contact`, `Stats`, ...), gives it `props`, and says when its hit lands:

    {"kind": "motion", "comp": "Contact", "at": <source second of the hit>, "hit": 0.55,
     "dur": 3.0, "props": {"distance": 238, ...}}

This module is the Python half. It knows which compositions exist and their default
length and hit time (`COMPS`, which `tests/test_editmotion.py` holds equal to what
Root.jsx registers), turns an overlay into a cacheable render job, and runs
`graphics/render.mjs` (one Node process per batch, so the bundle is built once) to
produce ProRes 4444 files with an alpha channel. The engine composites them like any
other overlay (`editfilters.overlay_chains`). Nothing here knows about clips.

**The invariant that matters: the composition is told how long it is and where its
hit is, and animates against those** (props `dur`, `hit`, `fps`). A count-up that
finishes on the hit, a stinger that lands on it, a bar that fills over the whole
length: all of it is relative, so the spec stays the one place that says when things
happen and a caller can lengthen a card without editing the graphics.
"""

import hashlib
import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .utils import StageError

GRAPHICS_DIR = Path(__file__).resolve().parent.parent / "graphics"
NATIVE_WIDTH, NATIVE_HEIGHT = 1920, 1080       # what every composition is drawn at (scaled for a preview)

# id -> default length and hit time, in output seconds. Keep equal to Root.jsx (a test does).
COMPS: Dict[str, Dict[str, float]] = {
    "Title": {"dur": 3.6, "hit": 1.0},
    "OpTitle": {"dur": 2.6, "hit": 0.5},
    "Contact": {"dur": 3.2, "hit": 0.55},
    "Stats": {"dur": 7.0, "hit": 0.6},
    "Acquire": {"dur": 6.0, "hit": 0.8},
    "Probe": {"dur": 1.0, "hit": 0.5},
}

# what each composition is and the props it reads (for `clipbot edit motion list`); the defaults live in Root.jsx
DOCS: Dict[str, Dict[str, str]] = {
    "Title": {"does": "the series card: a full-frame plate that builds to a slam on the hit (opaque; put it on a card)",
              "props": "series, number, kicker (typed line above), sub (line under the rule), foot (loading line)"},
    "OpTitle": {"does": "an operation header over live footage: tag, big title wiping in, rule, typed sub line",
                "props": "op (number), title, sub, label ('OPERATION'), x, y"},
    "Contact": {"does": "a contact report on a kill: the range counting up to a lock on a 0-300 m ruler, chips, who",
                "props": "index, total, distance (m, or omit when the feed gave none), headline, tone (kill|head|nade|multi|long), "
                         "victim, icon ('file:<png>'), tags [HEADSHOT, NADE, ...], hero (bigger), x, y"},
    "Stats": {"does": "the debrief card: stat tiles counting up, the operations ticked off, a stamp (opaque; put it on a card)",
              "props": "title, foot, stats [{label, value, unit, decimals}], ops [{n, name, count}], next"},
    "Acquire": {"does": "a range finder still working (digits scramble, nothing is claimed): the tease for a shot saved for later",
                "props": "label, line, x, y"},
    "Probe": {"does": "a bar and a word, for tests", "props": "text"},
}

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,40}$")
MAX_PROPS_CHARS = 8000


def known(comp: str) -> bool:
    return comp in COMPS


def defaults(comp: str) -> Dict[str, float]:
    return dict(COMPS[comp])


def check_props(props: Any, path: str) -> Dict[str, Any]:
    """`props` must be a JSON object of modest size (it is hashed, written to a jobs file and read by React)."""
    if props is None:
        return {}
    if not isinstance(props, dict):
        raise ValueError("{0}: props must be an object".format(path))
    try:
        blob = json.dumps(props, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("{0}: props are not JSON ({1})".format(path, exc))
    if len(blob) > MAX_PROPS_CHARS:
        raise ValueError("{0}: props are {1} characters, more than the {2} allowed".format(path, len(blob), MAX_PROPS_CHARS))
    return json.loads(blob)


@lru_cache(maxsize=1)
def source_hash() -> str:
    """A hash of everything a render depends on: the compositions, the fonts and images they use, the
    Remotion version. Part of every job's key, so editing a composition re-renders what it draws and
    nothing else does. Fonts are hashed by name and size (they are megabytes; they do not change by accident)."""
    h = hashlib.sha1()
    for sub in ("src", "public"):
        base = GRAPHICS_DIR / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(GRAPHICS_DIR).as_posix()
            h.update(rel.encode("utf-8"))
            if sub == "public":
                h.update(str(p.stat().st_size).encode("ascii"))
            else:
                h.update(p.read_bytes())
    pkg = GRAPHICS_DIR / "package.json"
    if pkg.is_file():
        h.update(pkg.read_bytes())
    return h.hexdigest()[:12]


def scale_for(width: int) -> float:
    """The Remotion scale a render of this output width is drawn at: 1, 1/2 or 1/4, the smallest that is not smaller
    than the output (ffmpeg scales the rest). A preview (960 wide) is drawn at a quarter of the pixels. Only these
    three, because Remotion refuses a scale that does not give whole-pixel dimensions (1/6 of 1080 is not 180)."""
    for s in (0.25, 0.5):
        if width <= NATIVE_WIDTH * s + 1e-6:
            return s
    return 1.0


def _file_stamps(value: Any, acc: List[Any]) -> List[Any]:
    """Size and mtime of every `file:<path>` in a props tree. render.mjs inlines those files, so an edited
    image must change the job's key even though the path in the props did not."""
    if isinstance(value, str) and value.startswith("file:"):
        try:
            st = Path(value[5:]).stat()
            acc.append([value, st.st_size, int(st.st_mtime)])
        except OSError:
            acc.append([value, -1, -1])
    elif isinstance(value, list):
        for v in value:
            _file_stamps(v, acc)
    elif isinstance(value, dict):
        for k in sorted(value):
            _file_stamps(value[k], acc)
    return acc


def make_job(overlay: Dict[str, Any], fps: float, width: int) -> Dict[str, Any]:
    """The render job for a normalised motion overlay (`kind`, `comp`, `props`, `dur`, `hit`)."""
    comp = overlay["comp"]
    props = overlay.get("props") or {}
    body = {"comp": comp, "props": props, "dur": round(float(overlay["dur"]), 4),
            "hit": round(float(overlay["hit"]), 4), "fps": float(fps), "scale": scale_for(width), "src": source_hash()}
    stamps = _file_stamps(props, [])
    if stamps:
        body["files"] = stamps
    key = hashlib.sha1(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return dict(body, key=key)


def out_path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / "motion" / "{0}.mov".format(key)


def find_node(settings: Any) -> str:
    """The Node binary graphics render with, or a StageError saying how to get one."""
    node = str(settings.tool("node")) if settings.get("tools.node") else "node"
    from shutil import which

    found = node if Path(node).is_file() else which(node)
    if not found:
        raise StageError("Motion graphics need Node 22+, and {0!r} was not found. Set tools.node in config/settings.json "
                         "(or CLIPBOT_NODE) to a node.exe; see CLAUDE.md, \"Motion graphics\".".format(node))
    return found


def check_ready(settings: Any) -> List[str]:
    """Problems that would stop a render (empty when ready): no Node, no packages. Offline, instant."""
    problems: List[str] = []
    try:
        find_node(settings)
    except StageError as exc:
        problems.append(str(exc))
    if not (GRAPHICS_DIR / "node_modules" / "remotion").is_dir():
        problems.append("graphics/node_modules is missing; run `npm install` in graphics/ with Node 22+ on the PATH")
    chrome = settings.get("tools.chrome")
    if chrome and not Path(str(chrome)).is_file():
        problems.append("tools.chrome points at {0}, which does not exist (set it to null to let Remotion fetch its own browser)".format(chrome))
    return problems


def render_missing(jobs: List[Dict[str, Any]], settings: Any, cache_dir: Path, log_path: Optional[Path] = None,
                   on_progress: Optional[Callable[[int, int, str], None]] = None,
                   cancelled: Optional[Callable[[], bool]] = None) -> int:
    """Render every job whose file is not in the cache yet (one Node process for all of them); returns how many it drew.

    A file is written as `.part.mov` and renamed by the script, so an interrupted run never leaves a file that
    looks finished. `on_progress(done, total, comp)` is called as jobs finish."""
    todo: Dict[str, Dict[str, Any]] = {}
    for j in jobs:
        if not out_path(cache_dir, j["key"]).is_file():
            todo.setdefault(j["key"], j)
    if not todo:
        return 0
    problems = check_ready(settings)
    if problems:
        raise StageError("cannot render motion graphics: " + "; ".join(problems))
    node = find_node(settings)
    (Path(cache_dir) / "motion").mkdir(parents=True, exist_ok=True)
    batch = {"chrome": (str(settings.get("tools.chrome")) if settings.get("tools.chrome") else None),
             "jobs": [dict(j, out=str(out_path(cache_dir, j["key"]))) for j in todo.values()]}
    jobs_file = Path(cache_dir) / "motion" / "jobs-{0}.json".format(os.getpid())
    jobs_file.write_text(json.dumps(batch), encoding="utf-8")
    argv = [node, str(GRAPHICS_DIR / "render.mjs"), str(jobs_file)]
    tail: List[str] = []
    done = 0
    proc = subprocess.Popen(argv, cwd=str(GRAPHICS_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, encoding="utf-8", errors="replace")
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            del tail[:-60]
            if log_path:
                with Path(log_path).open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("event") == "done":
                    done += 1
                    if on_progress:
                        on_progress(done, len(todo), str(msg.get("comp", "")))
            if cancelled and cancelled():
                proc.terminate()
                break
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stdout:
            proc.stdout.close()
        try:
            jobs_file.unlink()
        except OSError:
            pass
    if cancelled and cancelled():
        raise StageError("cancelled")
    missing = [j for j in todo.values() if not out_path(cache_dir, j["key"]).is_file()]
    if code != 0 or missing:
        raise StageError("the motion graphics render failed (exit {0}, {1} of {2} files missing):\n{3}".format(
            code, len(missing), len(todo), "\n".join(tail[-25:])))
    return done


def render_stills(items: List[Dict[str, Any]], settings: Any) -> None:
    """Frames of compositions as transparent PNGs - how graphics are inspected without a render. Each item is
    {comp, props, frame, out, dur, hit, fps=60, scale=1}; one Node process draws them all."""
    problems = check_ready(settings)
    if problems:
        raise StageError("cannot render motion graphics: " + "; ".join(problems))
    stills = []
    for it in items:
        d = defaults(it["comp"])
        stills.append({"comp": it["comp"], "props": it.get("props") or {}, "frame": int(it["frame"]), "out": str(it["out"]),
                       "dur": float(it.get("dur", d["dur"])), "hit": float(it.get("hit", d["hit"])),
                       "fps": float(it.get("fps", 60.0)), "scale": float(it.get("scale", 1.0))})
    batch = {"chrome": (str(settings.get("tools.chrome")) if settings.get("tools.chrome") else None), "stills": stills}
    jobs_file = Path(stills[0]["out"] + ".job.json")
    jobs_file.write_text(json.dumps(batch), encoding="utf-8")
    try:
        r = subprocess.run([find_node(settings), str(GRAPHICS_DIR / "render.mjs"), str(jobs_file)], cwd=str(GRAPHICS_DIR),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8",
                           errors="replace")
    finally:
        try:
            jobs_file.unlink()
        except OSError:
            pass
    missing = [s["out"] for s in stills if not Path(s["out"]).is_file()]
    if r.returncode != 0 or missing:
        raise StageError("the stills failed (exit {0}, {1} missing):\n{2}".format(r.returncode, len(missing), r.stdout[-1800:]))
