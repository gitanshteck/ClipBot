"""The `clipbot edit` and `clipbot killfeed` command groups.

Kept out of `cli.py` so that file stays the table of contents it is meant to be:
`cli.build_parser` calls `add_parsers(sub)` and nothing else here is imported by it.
Every command is a thin wrapper - the work is in `stages/edit.py`, `stages/killfeed.py`
and the pure `edit*.py` modules - so the dashboard can call the same functions.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import editcut, editdeliver, editqa, editspec, edittaste, edittemplates, edittimeline
from .config import Settings
from .progress import Progress
from .specerror import SpecError
from .stages import edit as edit_stage
from .stages import killfeed as killfeed_stage
from .utils import StageError, get_logger, human_size, resolve_tool
from .workspace import Workspace

log = get_logger("clipbot.edit")

SPEC_HELP = "a spec .json file, or the name of an edit under work/_edits/"


def _console_progress() -> Progress:
    state = {"last": ""}

    def sink(kind: str, payload: Dict[str, Any]) -> None:
        if kind == "phase" and payload["phase"] != state["last"]:
            state["last"] = payload["phase"]
            print("  [{0}]".format(payload["phase"]), file=sys.stderr, flush=True)

    return Progress(sink=sink)


def _spec_path(settings: Settings, ref: str) -> Path:
    p = Path(ref)
    if p.is_file():
        return p
    q = edit_stage.edit_dir(settings, ref) / "spec.json"
    if q.is_file():
        return q
    raise StageError("no spec file {0!r} and no edit named {0!r} under {1}".format(ref, edit_stage.edits_root(settings)))


def _edit_name(settings: Settings, ref: str) -> str:
    p = Path(ref)
    if p.is_file():
        return editspec.load(p)["name"]
    return ref


ASSET_ID_RE = re.compile(r"^(sfx|mus)_[0-9a-f]{16}$")


def _audio_ref(value: str) -> Dict[str, str]:
    """A library asset id (sfx_..., mus_...) or a file path -> a spec audio reference."""
    if ASSET_ID_RE.match(value):
        return {"asset": value}
    return {"file": str(Path(value).resolve())}


def _workspace(settings: Settings, name: str) -> Workspace:
    cand = Path(name)
    if not cand.is_dir():
        cand = Path(settings.work_root) / name
    if not cand.is_dir():
        raise StageError("no workspace {0!r}".format(name))
    return Workspace.open(cand).ensure()


# --------------------------------------------------------------------------
# killfeed
# --------------------------------------------------------------------------


def cmd_killfeed_scan(args: argparse.Namespace, settings: Settings) -> int:
    ws = _workspace(settings, args.workspace)
    path = killfeed_stage.scan_killfeed(ws, settings, player=args.player, force=args.force, keep_video=args.keep_video,
                                        progress=_console_progress())
    data = json.loads(path.read_text(encoding="utf-8"))
    print("{0}: {1} kill(s), {2} grenade, {3} headshot ({4})".format(
        path, data["kills"], data["nades"], data["headshots"], data["player"]))
    return 0


def cmd_killfeed_list(args: argparse.Namespace, settings: Settings) -> int:
    ws = _workspace(settings, args.workspace)
    data = killfeed_stage.load(ws)
    if data is None:
        raise StageError("{0} has no killfeed.json; run: clipbot killfeed scan --workspace {0}".format(args.workspace))
    for e in data["events"]:
        print("{0}  {1:>9.1f}s  {2:>5}  {3:<8}{4}{5}  {6}".format(
            e["id"], e["t"], "{0:.0f}m".format(e["dist_m"]) if e.get("dist_m") is not None else "?", e["weapon"],
            " headshot" if e.get("headshot") else "", " through-cover" if e.get("through_cover") else "", e.get("victim") or ""))
    return 0


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------


def _summary(timeline: Dict[str, Any], spec: Dict[str, Any]) -> List[str]:
    pace = edittimeline.pacing(timeline)
    lines = ["{0}: {1} clip(s), {2:.1f} s, {3}x{4} @ {5:g} fps, profile {6}".format(
        spec["name"], len(timeline["clips"]), timeline["duration"], spec["output"]["width"], spec["output"]["height"],
        spec["output"]["fps"], spec["profile"])]
    ch = editdeliver.chapter_lines(edittimeline.chapters(timeline), timeline["duration"])
    if ch["text"]:
        lines += ["chapters:"] + ["  " + l for l in ch["text"].splitlines()]
        lines += ["  (YouTube will chapter these)" if ch["qualifies"] else "  (not chapter-eligible: " + "; ".join(ch["problems"]) + ")"]
    lines.append("pacing: " + ("something new at least every {0:g} s".format(pace["max_gap"]) if not pace["dead"] else
                               "{0} dead stretch(es), longest {1:.1f} s".format(
                                   len(pace["dead"]), max(d["length"] for d in pace["dead"]))))
    # the dead stretches are counted above; the rest (a callout held too long, a flicker, an
    # opening with nothing in it, too many sound effects) are listed so `validate` shows what the gate saw
    lines += ["  pacing: " + i["detail"] for i in pace["issues"] if i["what"] != "dead"]
    for w in timeline["warnings"]:
        lines.append("warning: " + w)
    return lines


def cmd_edit_new(args: argparse.Namespace, settings: Settings) -> int:
    if args.template != "kill-montage":
        raise StageError("unknown template {0!r} (only kill-montage today)".format(args.template))
    sources: Dict[str, Dict[str, Any]] = {}
    kills: List[Dict[str, Any]] = []
    player = args.player or settings.get("killfeed.player") or "Player"
    for n, wsname in enumerate(args.killfeed, start=1):
        ws = _workspace(settings, wsname)
        data = killfeed_stage.load(ws)
        if data is None:
            raise StageError("{0} has no killfeed.json; run: clipbot killfeed scan --workspace {0}".format(wsname))
        alias = "v{0}".format(n)
        sources[alias] = {"workspace": ws.root.name}
        kills += edittemplates.kills_from_killfeed(data, alias, ws.root)
    if not kills:
        raise StageError("the kill feed(s) hold no kills for {0}".format(player))

    sfx = {}
    for kind, ref in (("hit", args.sfx_hit), ("nade", args.sfx_nade), ("headshot", args.sfx_headshot)):
        if ref:
            sfx[kind] = dict(_audio_ref(ref), gain_db=0.0)
    music = dict(_audio_ref(args.music), gain_db=args.music_gain) if args.music else None
    facts: Dict[str, Any] = {}
    spec = edittemplates.kill_montage(
        kills, sources, name=args.name, player=player, game=args.game or "", profile=args.profile, order=args.order,
        max_clips=args.max_clips, slowmo=args.slowmo, intro=not args.no_intro, outro=not args.no_outro, music=music,
        sfx=sfx or None, grade=args.grade, output_preset=args.preset, facts=facts)
    spec = editspec.normalize(spec)
    root = edit_stage.edit_dir(settings, args.name)
    path = root / "spec.json"
    if path.exists() and not args.force:
        raise StageError("{0} exists; pass --force to overwrite it (the old spec is kept as spec.previous.json)".format(path))
    root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(str(path), str(root / "spec.previous.json"))
    path.write_text(editspec.to_json(spec), encoding="utf-8")
    facts.update({"player": player, "game": args.game or "", "kills_available": len(kills)})
    (root / "facts.json").write_text(json.dumps(facts, indent=1), encoding="utf-8")
    timeline = edittimeline.compile_timeline(spec)
    print(path)
    print("\n".join(_summary(timeline, spec)))
    return 0


def cmd_edit_episode(args: argparse.Namespace, settings: Settings) -> int:
    """Build an episode spec from a lineup file: snap the cuts into pauses, match the clips'
    levels, run the `episode` template, and write spec.json, lineup.json (the prepared
    lineup - what the cuts moved to) and facts.json under work/_edits/<name>/."""
    lineup = json.loads(Path(args.lineup).read_text(encoding="utf-8"))
    lineup["name"] = args.name
    root = edit_stage.edit_dir(settings, args.name)
    path = root / "spec.json"
    if path.exists() and not args.force:
        raise StageError("{0} exists; pass --force to overwrite it (the old spec is kept as spec.previous.json)".format(path))

    def wav_for(alias: str) -> Any:
        src = lineup["sources"].get(alias) or {}
        if "workspace" not in src:
            return None
        wav = _workspace(settings, src["workspace"]).audio_path
        return wav if wav.is_file() else None

    report = editcut.prepare_lineup(lineup, wav_for, snap=not args.no_snap, match=not args.no_match)
    facts: Dict[str, Any] = {}
    spec = editspec.normalize(edittemplates.episode(lineup, facts=facts))
    root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(str(path), str(root / "spec.previous.json"))
    path.write_text(editspec.to_json(spec), encoding="utf-8")
    (root / "lineup.json").write_text(json.dumps(lineup, indent=1, ensure_ascii=False), encoding="utf-8")
    (root / "cuts.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for key in ("titles", "fact_lines", "credits", "hashtags", "game", "player", "notes"):
        if key in lineup:
            facts[key] = lineup[key]
    (root / "facts.json").write_text(json.dumps(facts, indent=1, ensure_ascii=False), encoding="utf-8")
    timeline = edittimeline.compile_timeline(spec)
    print(path)
    print("cuts: {0} clip(s) moved, {1} cut point(s) left as authored (no pause within reach)".format(
        report["moved"], report["unsnapped"]))
    print("\n".join(_summary(timeline, spec)))
    return 0


def cmd_edit_validate(args: argparse.Namespace, settings: Settings) -> int:
    try:
        spec = editspec.load(_spec_path(settings, args.spec))
    except SpecError as exc:
        print("INVALID: {0}".format(exc))
        return 1
    timeline = edittimeline.compile_timeline(spec)
    print("\n".join(_summary(timeline, spec)))
    return 0


def cmd_edit_render(args: argparse.Namespace, settings: Settings) -> int:
    path = _spec_path(settings, args.spec)
    spec = editspec.load(path)
    if args.preset:
        spec = editspec.normalize(dict(spec, output={"preset": args.preset}))
    report = edit_stage.render_edit(spec, settings, preview=args.preview, force=args.force, master=not args.no_master,
                                    progress=_console_progress())
    print(report["output"])
    print("{0:.1f} s, {1}, {2} clip(s), rendered in {3:.0f} s{4}".format(
        report["duration"], human_size(report["bytes"]), report["clips"], report["render_seconds"],
        "" if not report["loudness_target"] else "; mastered to {0:g} LUFS".format(report["loudness_target"])))
    for w in report["warnings"]:
        print("warning: " + w)
    return 0


def _render_paths(settings: Settings, name: str, preview: bool) -> Dict[str, Path]:
    root = edit_stage.edit_dir(settings, name)
    tag = "preview" if preview else "final"
    video = root / "render" / ("{0}.preview.mp4".format(name) if preview else "{0}.mp4".format(name))
    return {"root": root, "video": video, "timeline": root / "render" / "{0}.{1}.timeline.json".format(name, tag)}


def _rendered_spec(root: Path, name: str, preview: bool) -> Any:
    """The spec a render actually used (its output block may differ from spec.json's),
    else spec.json, else None."""
    for cand in (root / "render" / "{0}.{1}.spec.json".format(name, "preview" if preview else "final"), root / "spec.json"):
        if cand.is_file():
            return cand
    return None


def cmd_edit_verify(args: argparse.Namespace, settings: Settings) -> int:
    name = _edit_name(settings, args.spec)
    paths = _render_paths(settings, name, args.preview)
    if not paths["video"].is_file():
        raise StageError("{0} is not rendered yet (clipbot edit render {1}{2})".format(
            paths["video"], name, " --preview" if args.preview else ""))
    ffmpeg = resolve_tool(settings.tool("ffmpeg"), edit_stage.FFMPEG_HINT)
    ffprobe = resolve_tool(settings.tool("ffprobe"), edit_stage.FFMPEG_HINT)
    timeline = None
    if paths["timeline"].is_file():
        timeline = json.loads(paths["timeline"].read_text(encoding="utf-8"))["timeline"]
    master = None
    spec_file = _rendered_spec(paths["root"], name, args.preview)
    if spec_file is not None:
        master = editspec.load(spec_file)["master"]
    result = editqa.verify(ffmpeg, ffprobe, paths["video"], timeline, master, paths["root"] / "verify" /
                           ("preview" if args.preview else "final"), deep=not args.shallow)
    print(editqa.render_markdown(result))
    return 0 if result["ok"] else 1


def cmd_edit_inspect(args: argparse.Namespace, settings: Settings) -> int:
    spec = editspec.load(_spec_path(settings, args.spec))
    timeline = edittimeline.compile_timeline(spec)
    for e in timeline["clips"]:
        extra = []
        if e["type"] == "clip":
            extra.append("{0} {1:.1f}-{2:.1f}".format(e.get("src"), e["in"], e["out"]))
            if not e["plain"]:
                extra.append("retimed")
        if e["markers"]:
            extra.append("{0} marker(s)".format(len(e["markers"])))
        if e["overlays"]:
            extra.append("{0} overlay(s)".format(len(e["overlays"])))
        if e["transition_in"]["type"] != "cut" and e["index"] > 0:
            extra.append("in: " + e["transition_in"]["type"])
        print("{0:7.2f}-{1:7.2f}  {2:<8} {3:<10} {4}".format(e["start"], e["end"], e["id"], e["type"], "; ".join(extra)))
    print()
    print("\n".join(_summary(timeline, spec)))
    return 0


def cmd_edit_deliver(args: argparse.Namespace, settings: Settings) -> int:
    name = _edit_name(settings, args.spec)
    paths = _render_paths(settings, name, False)
    if not paths["video"].is_file() or not paths["timeline"].is_file():
        raise StageError("{0} is not rendered yet (clipbot edit render {1})".format(paths["video"], name))
    ffmpeg = resolve_tool(settings.tool("ffmpeg"), edit_stage.FFMPEG_HINT)
    timeline = json.loads(paths["timeline"].read_text(encoding="utf-8"))["timeline"]
    spec_file = _rendered_spec(paths["root"], name, False)
    if spec_file is None:
        raise StageError("no spec found for {0!r}; render it first".format(name))
    spec = editspec.load(spec_file)
    facts: Dict[str, Any] = {}
    facts_file = paths["root"] / "facts.json"
    if facts_file.is_file():
        facts = json.loads(facts_file.read_text(encoding="utf-8"))
    variants = [v for v in (args.variants or "").split(",") if v.strip()]
    manifest = editdeliver.build_package(
        ffmpeg, paths["video"], timeline, paths["root"] / "deliver", edittimeline.chapters(timeline), variants=variants,
        thumbs=args.thumbs if args.thumbs is not None else int(settings.get("edit.thumbnails", 3)),
        player=args.player or facts.get("player", ""), game=args.game or facts.get("game", ""),
        kills=int(facts.get("kills", sum(len(e["markers"]) for e in timeline["clips"]))), nades=int(facts.get("nades", 0)),
        far=facts.get("far"), src_wh=(spec["output"]["width"], spec["output"]["height"]),
        warnings=facts.get("notes", []), titles=facts.get("titles"), fact_lines=facts.get("fact_lines"),
        credits=facts.get("credits", []), hashtags=facts.get("hashtags"))
    for f in manifest["files"]:
        print("{0:<10} {1}  ({2})".format(f["kind"], f["file"], human_size(f["bytes"])))
    for w in manifest["warnings"]:
        print("note: " + w)
    return 0


def cmd_edit_list(args: argparse.Namespace, settings: Settings) -> int:
    root = edit_stage.edits_root(settings)
    if not root.is_dir():
        print("(no edits yet)")
        return 0
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        final = d / "render" / "{0}.mp4".format(d.name)
        state = "rendered {0}".format(human_size(final.stat().st_size)) if final.is_file() else "not rendered"
        print("{0:<32} {1}{2}".format(d.name, state, "" if (d / "spec.json").is_file() else "  (no spec)"))
    return 0


def cmd_edit_clean(args: argparse.Namespace, settings: Settings) -> int:
    name = _edit_name(settings, args.spec)
    cache = edit_stage.edit_dir(settings, name) / "cache"
    freed = 0
    if cache.is_dir():
        freed = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
        shutil.rmtree(str(cache))
    if args.segments:
        seg = edit_stage.cache_root(settings) / "seg"
        if seg.is_dir():
            freed += sum(f.stat().st_size for f in seg.rglob("*") if f.is_file())
            shutil.rmtree(str(seg))
    print("freed {0} (renders and specs are kept)".format(human_size(freed)))
    return 0


def _taste_file(settings: Settings) -> Path:
    return edit_stage.edits_root(settings) / "taste.json"


def cmd_edit_taste(args: argparse.Namespace, settings: Settings) -> int:
    path = _taste_file(settings)
    if args.action == "add":
        if not args.text:
            raise StageError("clipbot edit taste add 'the rule' [--said 'their words'] [--edit NAME]")
        r = edittaste.add(path, args.text, said=args.said or "", edit=args.edit or "")
        print("{0}: {1}".format(r["id"], r["text"]))
    elif args.action == "retire":
        if not args.text:
            raise StageError("clipbot edit taste retire <id>")
        r = edittaste.retire(path, args.text)
        print("retired {0}: {1}".format(r["id"], r["text"]))
    else:
        rules = edittaste.load(path)["rules"]
        shown = [r for r in rules if args.all or r.get("active", True)]
        if not shown:
            print("(no taste rules yet)")
        for r in shown:
            print("{0}{1}  {2}{3}".format(r["id"], "" if r.get("active", True) else " (retired)", r["text"],
                                          '   - they said: "{0}"'.format(r["said"]) if r.get("said") else ""))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def add_parsers(sub: Any) -> None:
    p_kf = sub.add_parser("killfeed", help="read a stream's kill feed: record one player's kills")
    kf = p_kf.add_subparsers(dest="killfeed_command", metavar="<action>", required=True)
    p = kf.add_parser("scan", help="OCR the kill feed and write killfeed.json (Windows OCR; ~1 s of work per 15 s of video)")
    p.add_argument("--workspace", required=True, help="workspace slug or path")
    p.add_argument("--player", help="in-game name (default killfeed.player)")
    p.add_argument("--force", action="store_true", help="re-scan even if killfeed.json exists")
    p.add_argument("--keep-video", action="store_true", help="YouTube: keep the scratch video track")
    p.set_defaults(func=cmd_killfeed_scan)
    p = kf.add_parser("list", help="print the kills in killfeed.json")
    p.add_argument("--workspace", required=True)
    p.set_defaults(func=cmd_killfeed_list)

    p_ed = sub.add_parser("edit", help="the edit engine: spec -> multi-source edit with retiming, overlays, music, mastering")
    ed = p_ed.add_subparsers(dest="edit_command", metavar="<action>", required=True)

    p = ed.add_parser("new", help="write a first-draft spec from a template")
    p.add_argument("name", help="edit name (a folder under work/_edits/)")
    p.add_argument("--template", default="kill-montage", choices=["kill-montage"])
    p.add_argument("--killfeed", nargs="+", required=True, metavar="WORKSPACE", help="workspace(s) with a killfeed.json")
    p.add_argument("--player")
    p.add_argument("--game")
    p.add_argument("--profile", default="punchy", choices=sorted(editspec.PROFILES))
    p.add_argument("--order", default="chapters", choices=["chapters", "chronological", "score"])
    p.add_argument("--max-clips", type=int)
    p.add_argument("--slowmo", default="nade", choices=["none", "nade", "special", "all"])
    p.add_argument("--grade", default="gamer_pop", choices=list(editspec.GRADE_PRESETS))
    p.add_argument("--preset", default="youtube_1080p60", choices=sorted(editspec.OUTPUT_PRESETS))
    p.add_argument("--music", help="music bed: a library asset id (mus_...) or a file; ducked under the game audio")
    p.add_argument("--music-gain", type=float, default=-18.0)
    p.add_argument("--sfx-hit", help="sound effect (asset id sfx_... or file) for an ordinary kill")
    p.add_argument("--sfx-nade", help="sound effect for a grenade kill")
    p.add_argument("--sfx-headshot", help="sound effect for a headshot")
    p.add_argument("--no-intro", action="store_true")
    p.add_argument("--no-outro", action="store_true")
    p.add_argument("--force", action="store_true", help="overwrite an existing spec")
    p.set_defaults(func=cmd_edit_new)

    p = ed.add_parser("episode", help="build an episode (cold open, sting, chapters, end card) from a lineup .json")
    p.add_argument("name", help="edit name (a folder under work/_edits/)")
    p.add_argument("lineup", help="the lineup file (see edittemplates.episode)")
    p.add_argument("--no-snap", action="store_true", help="keep every in/out exactly as written (no pause snapping)")
    p.add_argument("--no-match", action="store_true", help="do not match the clips' levels")
    p.add_argument("--force", action="store_true", help="overwrite an existing spec")
    p.set_defaults(func=cmd_edit_episode)

    p = ed.add_parser("validate", help="check a spec and print its timeline summary")
    p.add_argument("spec", help=SPEC_HELP)
    p.set_defaults(func=cmd_edit_validate)

    p = ed.add_parser("inspect", help="list a spec's clips on the timeline")
    p.add_argument("spec", help=SPEC_HELP)
    p.set_defaults(func=cmd_edit_inspect)

    p = ed.add_parser("render", help="render the edit (final quality, or --preview for a fast 540p proxy)")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--preview", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore cached clip renders")
    p.add_argument("--no-master", action="store_true", help="skip loudness mastering (quick look)")
    p.add_argument("--preset", choices=sorted(editspec.OUTPUT_PRESETS), help="render at this output preset instead of the spec's")
    p.set_defaults(func=cmd_edit_render)

    p = ed.add_parser("verify", help="check a render: length, A/V sync, loudness, dropouts, clicks; writes storyboard + loudness plot")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--preview", action="store_true")
    p.add_argument("--shallow", action="store_true", help="skip the slow black/freeze/silence scans")
    p.set_defaults(func=cmd_edit_verify)

    p = ed.add_parser("deliver", help="variants, thumbnails, chapters and post text for a finished render")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--variants", help="comma list of: " + ", ".join(sorted(editdeliver.VARIANTS)))
    p.add_argument("--thumbs", type=int, help="thumbnail count (default edit.thumbnails)")
    p.add_argument("--player")
    p.add_argument("--game")
    p.set_defaults(func=cmd_edit_deliver)

    p = ed.add_parser("list", help="list edits")
    p.set_defaults(func=cmd_edit_list)

    p = ed.add_parser("clean", help="delete an edit's cached clip renders (keeps spec and final render)")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--segments", action="store_true", help="also delete the shared source-segment cache")
    p.set_defaults(func=cmd_edit_clean)

    p = ed.add_parser("taste", help="the user's standing preferences for edits")
    p.add_argument("action", choices=["list", "add", "retire"])
    p.add_argument("text", nargs="?", help="the rule (add) or its id (retire)")
    p.add_argument("--said", help="the user's own words")
    p.add_argument("--edit", help="the edit that prompted it")
    p.add_argument("--all", action="store_true", help="include retired rules")
    p.set_defaults(func=cmd_edit_taste)
