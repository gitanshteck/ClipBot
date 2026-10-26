"""Versioned final renders and their changelog.

An edit gets re-rendered many times; without a record, "what changed since the one I watched?" has no answer and the
old render is overwritten. A *final* render now archives the one before it:

    render/<name>.mp4                      the live final (version `versions.json["current"]`)
    render/v<N>/<name>.v<N>.mp4            every earlier final, with the spec and timeline it was rendered from
                                           (and `lineup.v<N>.json` / `captions.v<N>.json` when the edit has them)
    versions.json                          {"current": N, "history": [{n, rendered_at, duration, bytes, from, summary, changes}]}
    changelog.md                           the same history, newest first, in words (editdiff)

Previews are not versioned: they are the quick look and are overwritten by design. Archiving moves files (same
volume, instant), and only after the new render is complete - a render that fails leaves the live final where it was.
Edits rendered before this existed have `render/v<N>/` folders made by hand; the live final is then version
`max(N) + 1`, which is what it was.
"""

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import editdiff

VERSION_DIR = re.compile(r"^v(\d+)$")


def render_dir(root: Path) -> Path:
    return Path(root) / "render"


def versions_path(root: Path) -> Path:
    return Path(root) / "versions.json"


def load(root: Path) -> Dict[str, Any]:
    p = versions_path(root)
    if p.exists():
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("history"), list):
                return doc
        except ValueError:
            pass
    return {"current": None, "history": []}


def _save(root: Path, doc: Dict[str, Any]) -> None:
    p = versions_path(root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def archived_numbers(root: Path) -> List[int]:
    d = render_dir(root)
    if not d.is_dir():
        return []
    return sorted(int(m.group(1)) for x in d.iterdir() if x.is_dir() for m in [VERSION_DIR.match(x.name)] if m)


def live_number(root: Path, name: str) -> Optional[int]:
    """The version number of the live final, or None when there is no live final."""
    if not (render_dir(root) / "{0}.mp4".format(name)).exists():
        return None
    cur = load(root).get("current")
    if cur:
        return int(cur)
    return max(archived_numbers(root) or [0]) + 1


def begin_final(root: Path, name: str) -> Dict[str, Any]:
    """Call before a final render starts writing: remember the spec the live final was rendered from (the render
    overwrites that record straight away). `{exists, n, spec}`."""
    rd = render_dir(root)
    exists = (rd / "{0}.mp4".format(name)).exists()
    spec = None
    p = rd / "{0}.final.spec.json".format(name)
    if exists and p.exists():
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            spec = None
    return {"exists": exists, "n": live_number(root, name) if exists else None, "spec": spec}


def _archive(root: Path, name: str, prev: Dict[str, Any]) -> int:
    """Move the previous live final into `render/v<N>/`; returns N."""
    rd = render_dir(root)
    n = int(prev["n"])
    while (rd / "v{0}".format(n)).exists() and any((rd / "v{0}".format(n)).glob("*.mp4")):
        n += 1                                    # that number is taken by an archive made by hand: take the next
    dest = rd / "v{0}".format(n)
    dest.mkdir(parents=True, exist_ok=True)
    os.replace(str(rd / "{0}.mp4".format(name)), str(dest / "{0}.v{1}.mp4".format(name, n)))
    tl = rd / "{0}.final.timeline.json".format(name)
    if tl.exists():
        shutil.copy2(str(tl), str(dest / "{0}.v{1}.timeline.json".format(name, n)))
    if prev.get("spec") is not None:
        (dest / "{0}.v{1}.spec.json".format(name, n)).write_text(json.dumps(prev["spec"], indent=1, ensure_ascii=False), encoding="utf-8")
    for stem in ("lineup", "captions"):
        src = Path(root) / "{0}.json".format(stem)
        out = dest / "{0}.v{1}.json".format(stem, n)
        if src.exists() and not out.exists():
            shutil.copy2(str(src), str(out))      # the files of *now*: close enough to what the previous render used to be useful
    return n


def commit_final(root: Path, name: str, prev: Dict[str, Any], new_spec: Dict[str, Any], report: Dict[str, Any],
                 part: Path, final: Path, now: Optional[float] = None) -> Dict[str, Any]:
    """The new final is complete at `part`: archive the live one, put the new one in its place, record the version
    and what changed. Returns the history entry."""
    n_prev: Optional[int] = None
    if prev.get("exists") and final.exists():
        n_prev = _archive(root, name, prev)
    Path(part).replace(final)
    doc = load(root)
    number = (n_prev + 1) if n_prev is not None else (max([int(h["n"]) for h in doc["history"]] + archived_numbers(root) + [0]) + 1)
    diff = editdiff.diff_specs(prev.get("spec"), new_spec)
    entry = {"n": number, "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now if now is not None else time.time())),
             "duration": report.get("duration"), "bytes": report.get("bytes"), "from": n_prev, "summary": editdiff.summary(diff),
             "changes": diff["lines"]}
    doc["history"] = [h for h in doc["history"] if int(h["n"]) != number] + [entry]
    doc["current"] = number
    _save(root, doc)
    write_changelog(root, name)
    return entry


def write_changelog(root: Path, name: str) -> Path:
    doc = load(root)
    lines = ["# {0}: changelog".format(name), ""]
    for h in sorted(doc["history"], key=lambda h: -int(h["n"])):
        dur = h.get("duration")
        lines.append("## v{0} - {1} - {2}".format(h["n"], h["rendered_at"].replace("T", " "),
                                                  "{0:.1f} s".format(dur) if dur else "?"))
        lines.append("")
        lines.append("{0}{1}".format("from v{0}: ".format(h["from"]) if h.get("from") else "", h.get("summary", "")))
        lines.append("")
        lines.extend("- {0}".format(c) for c in h.get("changes", []))
        lines.append("")
    p = Path(root) / "changelog.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _duration(timeline_path: Path) -> Optional[float]:
    try:
        return json.loads(timeline_path.read_text(encoding="utf-8"))["report"]["duration"]
    except (OSError, ValueError, KeyError):
        return None


def list_versions(root: Path, name: str) -> List[Dict[str, Any]]:
    """Every version that can be played, oldest first: archived ones (`which` = `v<N>`), the live final (`final`)
    and the preview. Each: `{which, n, file, bytes, duration, rendered_at, summary, changes}`."""
    rd = render_dir(root)
    hist = {int(h["n"]): h for h in load(root)["history"]}
    out: List[Dict[str, Any]] = []
    for n in archived_numbers(root):
        files = sorted((rd / "v{0}".format(n)).glob("*.mp4"))
        if not files:
            continue
        f = files[0]
        h = hist.get(n, {})
        out.append({"which": "v{0}".format(n), "n": n, "file": str(f), "bytes": f.stat().st_size,
                    "duration": h.get("duration") or _duration(f.with_name(f.name.replace(".mp4", ".timeline.json"))) or _duration(f.with_name(name + ".v{0}.timeline.json".format(n))),
                    "rendered_at": h.get("rendered_at") or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(f.stat().st_mtime)),
                    "summary": h.get("summary", ""), "changes": h.get("changes", [])})
    live = rd / "{0}.mp4".format(name)
    if live.exists():
        n = live_number(root, name)
        h = hist.get(n, {})
        out.append({"which": "final", "n": n, "file": str(live), "bytes": live.stat().st_size,
                    "duration": h.get("duration") or _duration(rd / "{0}.final.timeline.json".format(name)),
                    "rendered_at": h.get("rendered_at") or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(live.stat().st_mtime)),
                    "summary": h.get("summary", ""), "changes": h.get("changes", [])})
    prev = rd / "{0}.preview.mp4".format(name)
    if prev.exists():
        out.append({"which": "preview", "n": None, "file": str(prev), "bytes": prev.stat().st_size,
                    "duration": _duration(rd / "{0}.preview.timeline.json".format(name)),
                    "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(prev.stat().st_mtime)),
                    "summary": "the quick-look render", "changes": []})
    return out


def resolve_file(root: Path, name: str, which: str) -> Optional[Path]:
    """The mp4 for `preview`, `final` or `v<N>`; None when there is no such file."""
    for v in list_versions(root, name):
        if v["which"] == which:
            return Path(v["file"])
    return None
