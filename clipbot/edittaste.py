"""Taste memory: what the user has said about how their edits should look.

A rule file in `.claude/skills/clipbot-edit/` holds what is true of video in
general. This holds what is true of *this* user: "never zoom on the webcam",
"I hate the whoosh sound", "callouts bottom-left, not centre". Each entry keeps the
words the user used (`said`), so a later session can tell a rule from a guess and
retire it when it stops being true. The store is a small JSON file under
`work/_edits/`; nothing in the renderer reads it - it is for whoever is *writing the
spec* (the skill tells Claude to read it first and to apply it by hand), because a
taste is a judgement, and a judgement belongs in the spec where it can be seen.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .specerror import SpecError


def load(path: Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"version": 1, "rules": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SpecError("{0}: not valid JSON ({1})".format(p, exc))
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise SpecError("{0}: expected an object with a 'rules' list".format(p))
    return data


def save(path: Path, data: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def add(path: Path, text: str, said: str = "", edit: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """Record a rule. `said` is the user's own words; `edit` the edit that prompted it."""
    text = (text or "").strip()
    if not text:
        raise SpecError("a taste rule needs text")
    data = load(path)
    n = max([int(r["id"][1:]) for r in data["rules"] if str(r.get("id", "")).startswith("t") and str(r["id"])[1:].isdigit()] or [0]) + 1
    rule = {"id": "t{0}".format(n), "text": text, "said": said.strip(), "edit": edit,
            "added": time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time())), "active": True}
    data["rules"].append(rule)
    save(path, data)
    return rule


def retire(path: Path, rule_id: str) -> Dict[str, Any]:
    data = load(path)
    for r in data["rules"]:
        if r.get("id") == rule_id:
            r["active"] = False
            save(path, data)
            return r
    raise SpecError("no taste rule {0!r}".format(rule_id))


def active(path: Path) -> List[Dict[str, Any]]:
    return [r for r in load(path)["rules"] if r.get("active", True)]
