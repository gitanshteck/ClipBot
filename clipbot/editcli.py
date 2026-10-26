"""The `clipbot edit`, `clipbot killfeed`, `clipbot events` and `clipbot moments` command groups.

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

from . import audioevents, editcaptions, editcut, editdeliver, editmoment, editreview, editversions, editwords, library, editqa, editspec, edittaste, edittemplates, edittimeline, panns
from .config import Settings
from .progress import Progress
from .specerror import SpecError
from .stages import edit as edit_stage
from .stages import events as events_stage
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
# events (the reaction map)
# --------------------------------------------------------------------------


def cmd_events_fetch_model(args: argparse.Namespace, settings: Settings) -> int:
    dest = panns.checkpoint_path(settings)
    if panns.verify_checkpoint(dest):
        print("{0} is already here and matches the pinned MD5 {1}".format(dest, panns.CHECKPOINT_MD5))
        return 0
    print("fetching {0} ({1}, MD5 {2}) -> {3}".format(panns.CHECKPOINT_NAME, panns.CHECKPOINT_URL.split("?")[0], panns.CHECKPOINT_MD5, dest))
    last = {"mb": -1}

    def tick(done: int, total: Any) -> None:
        mb = done // (50 << 20)
        if mb != last["mb"]:
            last["mb"] = mb
            print("  {0:.0f} MB{1}".format(done / 1048576.0, " of {0:.0f}".format(total / 1048576.0) if total else ""), flush=True)

    panns.fetch_checkpoint(settings, progress=tick)
    print("ok: MD5 matches the record")
    return 0


def cmd_events_scan(args: argparse.Namespace, settings: Settings) -> int:
    ws = _workspace(settings, args.workspace)
    path = events_stage.scan_events(ws, settings, force=args.force, first=args.first, last=args.last, progress=_console_progress())
    doc = json.loads(path.read_text(encoding="utf-8"))
    print("{0}: {1} s tagged ({2}x realtime)".format(path, doc["seconds"], doc.get("realtime_factor")))
    for name, peaks in doc["peaks"].items():
        print("  {0:<10} {1:>4} peak(s) at >= {2:g}".format(name, len(peaks), doc["thresholds"][name]))
    return 0


def cmd_events_peaks(args: argparse.Namespace, settings: Settings) -> int:
    """The strongest peaks of one category, with the transcript line nearest each - to pick a moment that has no kill."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ws = _workspace(settings, args.workspace)
    doc = events_stage.load(ws)
    if doc is None:
        raise StageError("{0} has no events.json; run: clipbot events scan --workspace {0}".format(args.workspace))
    if args.category not in doc["categories"]:
        raise StageError("no category {0!r}; choose from {1}".format(args.category, ", ".join(doc["categories"])))
    transcript = ws.read_json(ws.transcript_path) if ws.transcript_path.exists() else {"segments": []}
    for pk in audioevents.top_peaks(doc, args.category, args.top, args.min_p, args.min_gap):
        near = editmoment.lines_between(transcript, pk["t"] - 6.0, pk["t"] + 6.0, limit=3)
        print("{0:>9}  {1:<8} p={2:.2f} mean={3:.2f} {4:>3.0f}s  {5}".format(
            editmoment._mmss(pk["t"]), args.category, pk["p"], pk["mean"], pk["end"] - pk["start"],
            " | ".join(l["text"] for l in near)))
    return 0


def cmd_events_check(args: argparse.Namespace, settings: Settings) -> int:
    """Measure a stream's reaction map against something that happened, so a number is trusted for what it is worth:
    gunfire against the kill record, laughter and shouting against the chat's laugh emotes (and against plain loudness,
    the signal the pipeline already had). Needs events.json, plus killfeed.json and/or a chat.json that is ok."""
    import numpy as np

    from .chatsync import audio_energy_envelope

    ws = _workspace(settings, args.workspace)
    doc = events_stage.load(ws)
    if doc is None:
        raise StageError("{0} has no events.json; run: clipbot events scan --workspace {0}".format(args.workspace))
    cats = {k: np.array(v) / 100.0 for k, v in doc["categories"].items()}
    n = doc["seconds"]
    printed = False
    kf = killfeed_stage.load(ws)
    if kf and kf.get("events"):
        printed = True
        times = [e["t"] for e in kf["events"]]
        res = audioevents.check_series({k: cats[k] for k in ("gunfire", "explosion")}, times, 2, 2)
        print("against {0} kills (the highest score within 2 s of each kill, against random moments):".format(len(times)))
        for k, r in res.items():
            print("  {0:<10} AUC {1:.2f}   >=0.25 at {2:.0%} of kills, {3:.0%} of random moments".format(k, r["auc"], r["at_moments"], r["at_random"]))
    chat = ws.read_json(ws.chat_path) if ws.chat_path.exists() else {}
    laughs = [t for t in audioevents.chat_laugh_times(chat, float(ws.read_state().get("chat_offset_seconds") or 0.0)) if 0 <= t < n]
    if len(laughs) >= 5:
        printed = True
        series = {k: cats[k] for k in ("laughter", "shout")}
        series["laughter|shout"] = np.maximum(cats["laughter"], cats["shout"])
        if ws.audio_path.exists():
            env = np.asarray(audio_energy_envelope(ws.audio_path, 1.0), dtype=float)[:n]
            series["loudness"] = (env - env.mean()) / (env.std() + 1e-9)
        res = audioevents.check_series(series, laughs, 12, 0)
        print("against {0} chat messages that laugh (the highest score in the 12 s before each; chance is AUC 0.50):".format(len(laughs)))
        for k, r in res.items():
            print("  {0:<15} AUC {1:.2f}".format(k, r["auc"]) + ("" if k == "loudness" else "   >=0.25 at {0:.0%} of them, {1:.0%} of random moments".format(r["at_moments"], r["at_random"])))
        peaks = audioevents.top_peaks(doc, "laughter", args.top, 0.0, 30.0)
        hits, listed = audioevents.top_hit_rate([p["t"] for p in peaks], laughs)
        print("the {0} strongest laughter peaks: chat laughed within 12 s after {1} of them ({2:.0%}); a random moment: {3:.0%}".format(
            listed, hits, hits / max(1, listed), audioevents.random_hit_rate(n, listed, laughs)))
    if not printed:
        print("nothing to check against: {0} has no kill record and under 5 chat laughs.".format(ws.slug))
    return 0


def cmd_events_tags(args: argparse.Namespace, settings: Settings) -> int:
    """What the model hears in one stretch (its ten strongest of 527 classes): the way to check a number."""
    ws = _workspace(settings, args.workspace)
    if not ws.audio_path.exists():
        raise StageError("{0} has no audio.wav".format(args.workspace))
    model = panns.load_model(panns.checkpoint_path(settings))
    names = panns.labels()
    a = float(args.at) - args.span / 2.0
    p = panns.tag_window(model, panns.read_wav(ws.audio_path, max(0.0, a), max(0.0, a) + args.span))
    for i in p.argsort()[::-1][:10]:
        print("  {0:.2f}  {1}".format(float(p[i]), names[int(i)]))
    return 0


# --------------------------------------------------------------------------
# moments
# --------------------------------------------------------------------------


def _all_workspaces(settings: Settings) -> List[Workspace]:
    root = Path(settings.work_root)
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and not d.name.startswith("_") and (d / "state.json").exists():
            out.append(Workspace.open(d))
    return out


def _arc_line(slug: str, a: Dict[str, Any]) -> str:
    ctx = a["context"]
    return "{0:<22} {1:<5} {2:>9} {3:>4.0f}s  lead {4:>3.0f}s tail {5:>3.0f}s  {6:>2} kill(s)  score {7:>5.1f}  {8}".format(
        slug[:22], a["id"], editmoment._mmss(a["start"]), ctx["length"], ctx["lead"], ctx["tail"], len(a["kills"]), a["score"],
        " ".join("{0}={1}".format(k, v) for k, v in a["parts"].items() if v))


def cmd_moments_build(args: argparse.Namespace, settings: Settings) -> int:
    ws = _workspace(settings, args.workspace)
    doc = editmoment.build(ws, settings, lead=args.lead, tail=args.tail, merge_gap=args.merge_gap, snap=not args.no_snap)
    print("{0}: {1} arc(s) from {2} kill(s) {3}".format(ws.slug, len(doc["arcs"]), doc["kills"], doc["kills_by_source"]))
    for a in editmoment.ranked(doc)[:args.top]:
        print(_arc_line(ws.slug, a))
    return 0


def cmd_moments_list(args: argparse.Namespace, settings: Settings) -> int:
    ws = _workspace(settings, args.workspace)
    doc = editmoment.load(ws)
    if doc is None:
        raise StageError("{0} has no moments.json; run: clipbot moments build --workspace {0}".format(args.workspace))
    arcs = editmoment.ranked(doc) if args.by_score else doc["arcs"]
    for a in arcs[:args.top]:
        print(_arc_line(ws.slug, a))
    return 0


def cmd_moments_sheet(args: argparse.Namespace, settings: Settings) -> int:
    ws = _workspace(settings, args.workspace)
    doc = editmoment.load(ws)
    if doc is None and not args.range:
        raise StageError("{0} has no moments.json; run: clipbot moments build --workspace {0}".format(args.workspace))
    arcs = doc["arcs"] if doc else []
    if args.range:
        arcs = []
        for r in args.range:
            try:
                a, b = (float(x) for x in r.split(","))
            except ValueError:
                raise StageError("--range is START,END in seconds, e.g. --range 1200,1260 (got {0!r})".format(r))
            arcs.append(editmoment.range_arc(ws, a, b))
    elif args.ids:
        want = set(args.ids.split(","))
        arcs = [a for a in arcs if a["id"] in want]
    else:
        arcs = editmoment.ranked(doc)[:args.top]
    resolver = None
    if ws.source_mode() == "embed" and ws.video_path() is None:
        from . import ytsegments

        resolver = ytsegments.StreamResolver(ws, settings)       # one stream resolution for every arc
    for a in arcs:
        out = ws.root / "moments" / "{0}.jpg".format(a["id"])
        if not args.force and out.exists():
            print("have {0}".format(out))
            continue
        try:
            print(editmoment.evidence_sheet(ws, settings, a, out, resolver=resolver))
        except StageError as exc:
            print("{0}: {1}".format(a["id"], exc), file=sys.stderr)
    return 0


def cmd_moments_show(args: argparse.Namespace, settings: Settings) -> int:
    """Arcs with everything the sheet cannot draw: the kills, the transcript lines (any script), what the analysis said."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # Devanagari on a cp1252 console
    ws = _workspace(settings, args.workspace)
    doc = editmoment.load(ws)
    if doc is None:
        raise StageError("{0} has no moments.json; run: clipbot moments build --workspace {0}".format(args.workspace))
    want = set(args.ids.split(","))
    events = events_stage.load(ws)
    for a in doc["arcs"]:
        if a["id"] not in want:
            continue
        print(_arc_line(ws.slug, a))
        print("  window {0:.1f}-{1:.1f}s  (edges moved into a pause: in {2}, out {3})".format(
            a["start"], a["end"], a["snapped"]["in"], a["snapped"]["out"]))
        for k in a["kills"]:
            print("  kill {0:>8.1f}s  {1:>5}  {2:<8}{3}{4}  [{5}]".format(
                k["t"], "{0:.0f}m".format(k["dist_m"]) if k.get("dist_m") is not None else "?", k["weapon"],
                " headshot" if k.get("headshot") else "", " through-cover" if k.get("through_cover") else "", k["source"]))
        for l in a["lines"]:
            print("  {0:>8.1f}s  {1}{2}".format(l["t"], l["text"], "  (low confidence)" if l.get("low") else ""))
        for c in a.get("analysis", []):
            print("  analysis {0:.0f}-{1:.0f}s: {2}".format(c["start"], c["end"], c["description"]))
        if events is not None:
            r = audioevents.reaction_between(events, a["start"], a["end"])
            print("  voice: {0}".format("{0} p={1:.2f} at {2}".format(r["category"], r["p"], editmoment._mmss(r["t"])) if r["p"] > 0 else "no laughter or shouting heard"))
    return 0


def cmd_moments_pool(args: argparse.Namespace, settings: Settings) -> int:
    import time

    rows = []
    for ws in _all_workspaces(settings):
        doc = editmoment.load(ws)
        if doc:
            rows += [(ws.slug, a) for a in doc["arcs"]]
    rows.sort(key=lambda r: -r[1]["score"])
    pool = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "arcs": [dict(a, workspace=slug) for slug, a in rows]}
    out = Path(settings.work_root) / "_moments" / "pool.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pool, indent=1), encoding="utf-8")
    print("{0} arc(s) from {1} workspace(s) -> {2}".format(len(rows), len({r[0] for r in rows}), out))
    for slug, a in rows[:args.top]:
        print(_arc_line(slug, a))
    return 0


def cmd_moments_coverage(args: argparse.Namespace, settings: Settings) -> int:
    print("{0:<24} {1:>5}  {2:>5} {3:>5}  {4:<16} {5}".format("workspace", "hours", "kills", "/h", "feed/popup/both", "verdict"))
    for ws in _all_workspaces(settings):
        row = editmoment.coverage(ws)
        if args.wardogs and not (row["scanned"] or "wardogs" in row["title"].lower()):
            continue
        src = row.get("sources") or {}
        print("{0:<24} {1:>5.1f}  {2:>5} {3:>5}  {4:<16} {5}   {6}".format(
            row["workspace"][:24], row["hours"], row.get("kills", "-"), row.get("per_hour") if row.get("per_hour") is not None else "-",
            "{0}/{1}/{2}".format(src.get("feed", 0), src.get("confirm", 0), src.get("both", 0)) if src else "-", row["verdict"], row["title"][:34]))
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
    _print_taste(settings)
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
    _print_taste(settings)
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

    def moments_for(alias: str) -> Any:
        src = lineup["sources"].get(alias) or {}
        return editmoment.load(_workspace(settings, src["workspace"])) if "workspace" in src else None

    def kills_for(alias: str) -> Any:
        src = lineup["sources"].get(alias) or {}
        if "workspace" not in src:
            return {}
        ws_ = _workspace(settings, src["workspace"])
        data = killfeed_stage.load(ws_)
        if not data:
            return {}
        return {k_["id"]: k_ for k_ in edittemplates.kills_from_killfeed(data, alias, ws_.root) if k_.get("id")}

    filled = edittemplates.expand_moments(lineup, moments_for, kills_for)
    for line in filled:
        print("moment: " + line)
    for style_id, style in ((lineup.get("brand") or {}).get("styles") or {}).items():
        library.save_text_style(settings, style_id, style)     # the brand's typefaces, idempotently
    words_cb = None
    if getattr(args, "words", False):
        def words_cb(alias: str, a: float, b: float) -> Any:
            src = lineup["sources"].get(alias) or {}
            if "workspace" not in src:
                return None
            return editwords.words_for(settings, _workspace(settings, src["workspace"]), a, b, fetch=True)

        print("words: fetching word timing where it is not already stored (OpenAI whisper-1, about $0.006 a minute)")
    report = editcut.prepare_lineup(lineup, wav_for, snap=not args.no_snap, match=not args.no_match, words_for=words_cb)
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
    for r in report["clips"]:
        for kind in ("in", "out"):
            if r.get("inside_" + kind):
                print("INSIDE A WORD: {0} {1} {2:.2f}s splits {3!r} and no clean place is within 0.6 s".format(
                    r["src"], kind, r[kind], r["inside_" + kind]))
    issues = edittemplates.context_issues(lineup)
    for issue in issues:
        print("CONTEXT: " + issue)
    if issues:
        print("{0} kill clip(s) without their lead-up or aftermath: widen them, or mark a deliberate insert \"cutaway\": true".format(len(issues)))
    for w in facts.get("warnings", []):
        print("BEAT: " + w)
    print("\n".join(_summary(timeline, spec)))
    return 0


def cmd_edit_beats(args: argparse.Namespace, settings: Settings) -> int:
    """What the lineup can ask for: named moves for a clip's `beats`, transition presets for a chapter's `transition`."""
    from . import editbeats

    print("beats (a lineup clip's \"beats\": [{\"do\": NAME, \"at\": source second, ...}]):")
    for name, m in editbeats.MACROS.items():
        print("  {0:<12} {1}\n  {2:<12} {3}".format(name, m["does"], "", m["params"]))
    print("\ntransitions (a lineup chapter's \"transition\": NAME):")
    for name, t in editbeats.TRANSITIONS.items():
        print("  {0:<13}{1}".format(name, t["does"]))
    print("\nsounds (the lineup's \"beat_sfx\": {role: {asset, gain_db}}): " + ", ".join(editbeats.SFX_ROLES))
    return 0


def cmd_edit_motion(args: argparse.Namespace, settings: Settings) -> int:
    """The motion graphics a spec's `motion` overlay can name (`list`), or one frame of one drawn to a transparent PNG
    (`still`) - how a graphic is looked at without rendering an edit."""
    from . import editmotion

    if args.motion_cmd == "list":
        print("motion graphics (an overlay {\"kind\": \"motion\", \"comp\": NAME, \"at\": source second the graphic lands, \"props\": {...}}):")
        for name, d in editmotion.COMPS.items():
            doc = editmotion.DOCS.get(name, {})
            print("  {0:<9} {1:.1f} s, hits at {2:.2f} s - {3}\n  {4:<9} props: {5}".format(
                name, d["dur"], d["hit"], doc.get("does", ""), "", doc.get("props", "")))
        problems = editmotion.check_ready(settings)
        print("\nready: " + ("yes" if not problems else "NO - " + "; ".join(problems)))
        return 0
    if args.comp not in editmotion.COMPS:
        raise StageError("no motion graphic {0!r} (one of: {1})".format(args.comp, ", ".join(sorted(editmotion.COMPS))))
    props = json.loads(Path(args.props).read_text(encoding="utf-8")) if args.props else {}
    d = editmotion.defaults(args.comp)
    out = Path(args.out or "{0}_{1}.png".format(args.comp, args.frame))
    editmotion.render_stills([{"comp": args.comp, "props": props, "frame": args.frame, "out": out.resolve(), "dur": args.dur or d["dur"],
                               "hit": args.hit if args.hit is not None else d["hit"], "fps": args.fps}], settings)
    print(out.resolve())
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
    words = None
    wf = paths["root"] / "words.json"
    if wf.is_file():
        words = json.loads(wf.read_text(encoding="utf-8")).get("clips")
    result = editqa.verify(ffmpeg, ffprobe, paths["video"], timeline, master, paths["root"] / "verify" /
                           ("preview" if args.preview else "final"), deep=not args.shallow, words=words)
    print(editqa.render_markdown(result))
    return 0 if result["ok"] else 1


def cmd_edit_scenes(args: argparse.Namespace, settings: Settings) -> int:
    """Where the webcam was, per zoomed clip, as the last render saw it - and a sheet to check it by eye."""
    name = _edit_name(settings, args.spec)
    paths = _render_paths(settings, name, args.preview)
    if not paths["timeline"].is_file():
        raise StageError("{0} has not been rendered yet (clipbot edit render {0}{1}); the scene map is made during a render".format(
            name, " --preview" if args.preview else ""))
    timeline = json.loads(paths["timeline"].read_text(encoding="utf-8"))["timeline"]
    n = 0
    for e in timeline["clips"]:
        if e.get("type") != "clip" or not e.get("scene"):
            continue
        n += 1
        spans = "; ".join("{0:.1f}-{1:.1f}s {2}".format(s["t0"], s["t1"],
                          "moving" if s.get("unstable") else "no webcam" if s["cam"] is None else
                          "webcam at x{0:.2f} y{1:.2f} {2:.2f}x{3:.2f}".format(s["cam"]["x"], s["cam"]["y"], s["cam"]["w"], s["cam"]["h"]))
                          for s in e["scene"])
        print("{0:<8} {1}".format(e["id"], spans))
    if not n:
        print("no clip in {0} zooms, so none was mapped".format(name))
        return 0
    if paths["video"].is_file():
        ffmpeg = resolve_tool(settings.tool("ffmpeg"), edit_stage.FFMPEG_HINT)
        sheets = editqa.webcam_sheet(ffmpeg, paths["video"], timeline, paths["root"] / "verify" / ("preview" if args.preview else "final"))
        for f in sheets:
            print("sheet: " + f)
    return 0


def _spec_clip_windows(settings: Settings, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every clip of a spec that has a workspace source, with that workspace: [{id, ws, in, out}]."""
    out = []
    for c in spec["clips"]:
        if c["type"] != "clip":
            continue
        src = spec["sources"].get(c["src"]) or {}
        if "workspace" not in src:
            continue
        root = Path(settings.work_root) / src["workspace"]
        if root.is_dir():
            out.append({"id": c["id"], "ws": Workspace.open(root), "in": float(c["in"]), "out": float(c["out"])})
    return out


def cmd_edit_words(args: argparse.Namespace, settings: Settings) -> int:
    """Fetch word timing for every clip of an edit (once; shared between edits), then report every cut that
    would split a word. The only network call is OpenAI whisper-1 on the clips' own audio, at about
    $0.006 a minute - the same model the transcribe stage uses by default."""
    spec = editspec.load(_spec_path(settings, args.spec))
    name = spec["name"]
    clips = _spec_clip_windows(settings, spec)
    if not clips:
        raise StageError("{0} has no clip with a workspace source to take words from".format(name))
    seconds = sum(c["out"] - c["in"] + 2 * editwords.PAD for c in clips)
    print("{0} clip(s), about {1:.1f} min of audio: about ${2:.2f} on {3} (already-fetched windows are free)".format(
        len(clips), seconds / 60.0, editwords.estimate_cost(seconds), settings.get("words.model", "whisper-1")))
    words_by_clip: Dict[str, Any] = {}
    inside = []
    for c in clips:
        words = editwords.words_for(settings, c["ws"], c["in"], c["out"], fetch=not args.check_only)
        if words is None:
            print("  {0}: not fetched (--check-only)".format(c["id"]))
            continue
        words_by_clip[c["id"]] = words
        for kind, t in (("in", c["in"]), ("out", c["out"])):
            snap = editwords.word_snap(words, t, kind)
            if snap["inside"]:
                inside.append((c["id"], kind, t, snap))
    root = edit_stage.edit_dir(settings, name)
    root.mkdir(parents=True, exist_ok=True)
    (root / "words.json").write_text(json.dumps({"version": 1, "clips": words_by_clip}, ensure_ascii=False), encoding="utf-8")
    n_words = sum(len(v) for v in words_by_clip.values())
    print("{0} words in {1} clip(s) -> {2}".format(n_words, len(words_by_clip), root / "words.json"))
    silent = [cid for cid, v in words_by_clip.items() if not v]
    if silent:
        print("NO WORDS HEARD in {0}: the recogniser found no speech there (game noise, or speech it would not write); "
              "write its captions into captions.json by hand if there are any".format(", ".join(silent)))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for cid, kind, t, snap in inside:
        print("INSIDE A WORD: {0} {1} at {2:.2f}s splits {3!r}; {4}".format(
            cid, kind, t, snap["inside"], "would move {0:+.2f}s".format(snap["moved"]) if snap["snapped"] else "no clean place within 0.6 s"))
    print("{0} of {1} cut point(s) land inside a word".format(len(inside), 2 * len(words_by_clip)))
    return 0


def _output_clock(timeline: Dict[str, Any], clip_id: str, cue: Dict[str, Any]) -> float:
    """Where in the finished video (seconds) a cue given in the stream's seconds is heard."""
    e = next(c for c in timeline["clips"] if c["id"] == clip_id)
    return e["start"] + edittimeline.src_to_out(e["pieces"], cue["s"])


def cmd_edit_captions(args: argparse.Namespace, settings: Settings) -> int:
    """Write the captions for an edit: each clip's words -> Hinglish subtitle chunks (one model call per clip) ->
    work/_edits/<name>/captions.json, which is yours to edit before rendering. Needs `edit words` first and
    ANTHROPIC_API_KEY (the same key `analyze` uses; `captions.model`, a few cents for an episode)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    spec = editspec.load(_spec_path(settings, args.spec))
    name = spec["name"]
    root = edit_stage.edit_dir(settings, name)
    path = root / "captions.json"
    old_doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    want = set(args.clips.split(",")) if args.clips else None
    if old_doc and want is None and not args.force:
        raise StageError("{0} exists (it may hold your edits); pass --force to rewrite all of it, or --clips c1,c2 to redo some".format(path))
    clips: Dict[str, Dict[str, Any]] = {}
    for c in _spec_clip_windows(settings, spec):
        if want is not None and c["id"] not in want:
            continue
        words = editwords.words_for(settings, c["ws"], c["in"], c["out"], fetch=False)
        if words is None:
            print("  {0}: no words stored (run: clipbot edit words {1})".format(c["id"], name))
            continue
        if not words:
            print("  {0}: no speech heard, so no captions (add some to captions.json by hand if there is any)".format(c["id"]))
            continue
        clips[c["id"]] = {"words": words, "in": c["in"], "out": c["out"]}
    if not clips:
        raise StageError("no clip has words to caption; run: clipbot edit words {0}".format(name))
    if args.no_model:
        doc = {"version": editcaptions.SCHEMA, "model": None, "clips": {
            cid: {"cues": editcaptions.cues_from_chunks(editcaptions.fallback_chunks(c["words"]), c["words"], c["in"], c["out"]),
                  "chunks": 0, "fallback": True} for cid, c in clips.items()}}
    else:
        print("captions: {0} clip(s), {1} words, one model call each".format(len(clips), sum(len(c["words"]) for c in clips.values())))
        doc = editcaptions.generate(settings, clips)
    if old_doc and want is not None:                 # redo some clips, keep the rest (and any hand edits to them)
        merged = dict(old_doc.get("clips") or {})
        merged.update(doc["clips"])
        doc["clips"] = merged
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    timeline = edittimeline.compile_timeline(spec)
    cues = [(cid, q) for cid, v in doc["clips"].items() for q in v["cues"]]
    fell = [cid for cid, v in doc["clips"].items() if v.get("fallback")]
    print("{0} cue(s) in {1} clip(s) -> {2}".format(len(cues), len(doc["clips"]), path))
    if fell:
        print("FELL BACK to the recogniser's own text (no model answer): {0}".format(", ".join(fell)))
    if cues:
        longest = max(len(q["text"]) for _, q in cues)
        print("longest cue {0} characters (limit {1})".format(longest, editcaptions.MAX_CHARS))
    hits = []
    for cid, q in cues:
        found = editwords.strong_words(q["text"])
        if found:
            hits.append((_output_clock(timeline, cid, q), cid, q["text"], found))
    for t, cid, text, found in sorted(hits)[:20]:
        flag = "IN THE FIRST 30 s: " if t < 30.0 else ""
        print("{0}strong language at {1}:{2:04.1f} ({3}): {4!r}  [{5}]".format(
            flag, int(t // 60), t % 60, cid, text, ", ".join(found)))
    if not hits:
        print("no strong language in the captions")
    return 0


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


def cmd_edit_thumbnail(args: argparse.Namespace, settings: Settings) -> int:
    """A designed thumbnail from the final render: a frame at `--at` seconds of the video, graded, with
    the words on the side opposite the face."""
    name = _edit_name(settings, args.spec)
    paths = _render_paths(settings, name, False)
    if not paths["video"].is_file():
        raise StageError("{0} is not rendered yet (clipbot edit render {1})".format(paths["video"], name))
    ffmpeg = resolve_tool(settings.tool("ffmpeg"), edit_stage.FFMPEG_HINT)
    out_dir = paths["root"] / "deliver" / "thumbnails"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = out_dir / "_frame.png"
    if not editqa._extract_frame(ffmpeg, paths["video"], args.at, frame, 1920):
        raise StageError("could not read a frame at {0} s".format(args.at))
    font = None
    if args.font:
        assets = library.asset_map(settings)
        if args.font not in assets:
            raise StageError("no library font {0!r} (clipbot library list)".format(args.font))
        font = str(assets[args.font]["path"])
    out = Path(args.out) if args.out else out_dir / "designed_{0}.jpg".format(len(list(out_dir.glob("designed_*.jpg"))) + 1)
    focus = None
    if args.focus:
        try:
            fx, fy = (float(v) for v in args.focus.split(","))
        except ValueError:
            raise StageError("--focus is X,Y as fractions of the frame, e.g. 0.45,0.4")
        focus = (fx, fy)
    result = editdeliver.compose_thumbnail(frame, args.text, out, font=font, accent=args.accent, side=args.side,
                                           zoom=args.zoom, focus=focus)
    frame.unlink()
    print("{0}  ({1}, text {2}, legibility {3})".format(result["file"], human_size(result["bytes"]),
                                                         " / ".join(result["lines"]), result["legibility"]))
    if result["legibility"] < 0.5:
        print("warning: the words are low-contrast at sidebar size (legibility {0} < 0.5)".format(result["legibility"]))
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


def _print_taste(settings: Settings) -> None:
    """The user's standing preferences, printed where a spec is about to be written: nothing in the renderer reads
    them (a taste is a judgement and belongs in the spec where it can be seen), so the author has to."""
    rules = edittaste.active(_taste_file(settings))
    if rules:
        print("taste rules in force (apply them by hand; `clipbot edit taste list` says what was said):")
        for r in rules:
            print("  {0}  {1}".format(r["id"], r["text"]))


# --------------------------------------------------------------------------
# review: comments on a render, versions and their changelog
# --------------------------------------------------------------------------


def _edit_root(settings: Settings, name: str) -> Path:
    root = edit_stage.edit_dir(settings, name)
    if not root.is_dir():
        raise StageError("no edit named {0!r} under {1}".format(name, edit_stage.edits_root(settings)))
    return root


def cmd_edit_versions(args: argparse.Namespace, settings: Settings) -> int:
    root = _edit_root(settings, args.name)
    vs = editversions.list_versions(root, args.name)
    if not vs:
        print("{0} has no renders yet".format(args.name))
        return 0
    live = editversions.live_number(root, args.name)
    for v in vs:
        label = {"final": "v{0} (live)".format(live), "preview": "preview"}.get(v["which"], v["which"])
        print("{0:<10} {1:>9}  {2:>7.1f} s  {3:>6.0f} MB  {4}".format(
            label, v["rendered_at"].replace("T", " ")[:16], v["duration"] or 0.0, (v["bytes"] or 0) / 1048576.0, v["summary"]))
        if args.changes:
            for c in v["changes"][:args.changes]:
                print("             - {0}".format(c))
    return 0


def cmd_edit_review(args: argparse.Namespace, settings: Settings) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = _edit_root(settings, args.name)
    doc = editreview.load(root, args.name)
    act = args.review_cmd
    if act == "list":
        shown = doc["comments"] if args.all else editreview.open_comments(doc)
        if args.on:
            shown = [c for c in shown if c.get("on") == args.on]
        if not shown:
            print("no {0}comments on {1}".format("" if args.all else "open ", args.name))
        for c in shown:
            print(editreview.line(c))
            if c.get("reply"):
                print("       reply: {0}".format(c["reply"]))
        return 0
    if act == "add":
        c = editreview.add(doc, args.at, args.text, kind=args.kind, on=args.on, timeline=editreview.timeline_for(root, args.name, args.on))
        editreview.save(root, doc)
        print(editreview.line(c))
        return 0
    if act == "taste":
        c = next((x for x in doc["comments"] if x["id"] == args.id), None)
        if c is None:
            raise StageError("no comment {0!r}".format(args.id))
        rule = edittaste.add(_taste_file(settings), args.text or c["text"], said=c["text"], edit=args.name)
        editreview.resolve(doc, args.id, how="taste rule {0}".format(rule["id"]), in_version="")
        editreview.save(root, doc)
        print("{0}: {1}   (comment {2} done)".format(rule["id"], rule["text"], args.id))
        return 0
    if act == "resolve":
        c = editreview.resolve(doc, args.id, how=args.how or "", in_version=args.version, status="wontfix" if args.wontfix else "done")
    elif act == "reopen":
        c = editreview.reopen(doc, args.id)
    elif act == "reply":
        c = editreview.reply(doc, args.id, args.text)
    else:
        editreview.delete(doc, args.id)
        editreview.save(root, doc)
        print("deleted {0}".format(args.id))
        return 0
    editreview.save(root, doc)
    print(editreview.line(c))
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

    p_ev = sub.add_parser("events", help="the reaction map: tag a stream's audio (laughter, shouting, gunfire, music) second by second")
    ev = p_ev.add_subparsers(dest="events_cmd", required=True)
    p = ev.add_parser("fetch-model", help="download the PANNs checkpoint (359 MB, zenodo.org, MD5-pinned) - the only command that does")
    p.set_defaults(func=cmd_events_fetch_model)
    p = ev.add_parser("scan", help="tag audio.wav and write events.json (needs PyTorch and the checkpoint)")
    p.add_argument("--workspace", required=True, help="workspace slug or path")
    p.add_argument("--first", type=int, help="start at this second (to try the model on a stretch first)")
    p.add_argument("--last", type=int, help="stop at this second")
    p.add_argument("--force", action="store_true", help="re-scan even if events.json exists")
    p.set_defaults(func=cmd_events_scan)
    p = ev.add_parser("peaks", help="a category's strongest peaks, with the transcript nearby")
    p.add_argument("--workspace", required=True)
    p.add_argument("--category", default="laughter", help="laughter, shout, gunfire, explosion, cheer, music, speech or silence")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--min-p", type=float, default=0.0, help="only peaks scoring at least this (0-1)")
    p.add_argument("--min-gap", type=float, default=20.0, help="seconds between two listed peaks")
    p.set_defaults(func=cmd_events_peaks)
    p = ev.add_parser("check", help="measure the map against the kill record (gunfire) and the chat's laugh emotes (laughter, shouting) - and against plain loudness")
    p.add_argument("--workspace", required=True)
    p.add_argument("--top", type=int, default=20, help="how many of the strongest laughter peaks to score (default 20)")
    p.set_defaults(func=cmd_events_check)
    p = ev.add_parser("tags", help="the model's ten strongest classes for a few seconds around a time (to check a number)")
    p.add_argument("--workspace", required=True)
    p.add_argument("--at", type=float, required=True, help="stream second")
    p.add_argument("--span", type=float, default=4.0, help="seconds of audio to listen to (default 4)")
    p.set_defaults(func=cmd_events_tags)

    p_mo = sub.add_parser("moments", help="build whole moments from kills, rank them, and write a contact sheet of each to choose from")
    mo = p_mo.add_subparsers(dest="moments_cmd", required=True)
    p = mo.add_parser("build", help="kills -> arcs (lead-up, kills, aftermath), ranked; writes <workspace>/moments.json")
    p.add_argument("--workspace", required=True, help="workspace slug or path")
    p.add_argument("--lead", type=float, help="seconds before the first kill (default moments.lead, %g)" % editmoment.LEAD)
    p.add_argument("--tail", type=float, help="seconds after the last kill (default moments.tail, %g)" % editmoment.TAIL)
    p.add_argument("--merge-gap", type=float, help="kills closer than this are one arc (default %g)" % editmoment.MERGE_GAP)
    p.add_argument("--no-snap", action="store_true", help="do not move the edges into a pause in the speech")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_moments_build)
    p = mo.add_parser("list", help="print a workspace's arcs")
    p.add_argument("--workspace", required=True)
    p.add_argument("--top", type=int, default=100)
    p.add_argument("--by-score", action="store_true", help="best first (default: in time order)")
    p.set_defaults(func=cmd_moments_list)
    p = mo.add_parser("sheet", help="write a contact sheet per arc to <workspace>/moments/<id>.jpg (a YouTube arc is fetched first)")
    p.add_argument("--workspace", required=True)
    p.add_argument("--ids", help="comma list of arc ids (default: the --top best)")
    p.add_argument("--range", action="append", metavar="START,END", help="a stretch by hand, in seconds (repeatable); no moments.json needed")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--force", action="store_true", help="redraw sheets that exist")
    p.set_defaults(func=cmd_moments_sheet)
    p = mo.add_parser("show", help="print arcs in full: kills, transcript lines in any script, what the analysis said")
    p.add_argument("--workspace", required=True)
    p.add_argument("--ids", required=True, help="comma list of arc ids")
    p.set_defaults(func=cmd_moments_show)
    p = mo.add_parser("pool", help="every workspace's arcs, best first, across streams; writes work/_moments/pool.json")
    p.add_argument("--top", type=int, default=30)
    p.set_defaults(func=cmd_moments_pool)
    p = mo.add_parser("coverage", help="which streams have a trustworthy kill record, and which need a (re)scan")
    p.add_argument("--wardogs", action="store_true", help="only streams that were scanned or whose title says Wardogs")
    p.set_defaults(func=cmd_moments_coverage)

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
    p.add_argument("--no-snap", action="store_true", help="no pause snapping (clips keep their in/out); `--words` still moves a cut that splits a word")
    p.add_argument("--no-match", action="store_true", help="do not match the clips' levels")
    p.add_argument("--words", action="store_true", help="also move a cut that would split a word to the word's edge (fetches word timing; ~$0.006/min)")
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

    p = ed.add_parser("scenes", help="where the webcam was in each zoomed clip (from the last render) and a sheet to check it by eye")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--preview", action="store_true")
    p.set_defaults(func=cmd_edit_scenes)

    p = ed.add_parser("words", help="word timing for every clip (OpenAI whisper-1, ~$0.006/min, cached) and the cuts that split a word")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--check-only", action="store_true", help="use only what is already fetched; send nothing")
    p.set_defaults(func=cmd_edit_words)

    p = ed.add_parser("captions", help="Hinglish subtitles per clip from its words (one model call each) -> captions.json to edit")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--force", action="store_true", help="rewrite captions.json even if it exists")
    p.add_argument("--clips", help="redo only these clip ids (comma list), keeping the rest of captions.json")
    p.add_argument("--no-model", action="store_true", help="use the recogniser's own text (no API call)")
    p.set_defaults(func=cmd_edit_captions)

    p = ed.add_parser("deliver", help="variants, thumbnails, chapters and post text for a finished render")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--variants", help="comma list of: " + ", ".join(sorted(editdeliver.VARIANTS)))
    p.add_argument("--thumbs", type=int, help="thumbnail count (default edit.thumbnails)")
    p.add_argument("--player")
    p.add_argument("--game")
    p.set_defaults(func=cmd_edit_deliver)

    p = ed.add_parser("thumbnail", help="a designed thumbnail from the final render: a graded frame plus bold words")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--at", type=float, required=True, help="seconds into the rendered video to take the frame from")
    p.add_argument("--text", required=True, help="1-3 lines of words (4-6 words reads best)")
    p.add_argument("--side", default="right", choices=["left", "right"], help="which side the words go (opposite the face)")
    p.add_argument("--accent", default="#35D0FF", help="accent colour for the last line and the frame")
    p.add_argument("--font", help="a library font asset id (default: the system display font)")
    p.add_argument("--zoom", type=float, default=1.0, help="crop closer (1.2-1.6) so the person can sit opposite the words")
    p.add_argument("--focus", help="X,Y of the person as fractions of the frame (with --zoom), e.g. 0.45,0.4")
    p.add_argument("--out", help="output .jpg (default deliver/thumbnails/designed_N.jpg)")
    p.set_defaults(func=cmd_edit_thumbnail)

    p = ed.add_parser("list", help="list edits")
    p.set_defaults(func=cmd_edit_list)

    p = ed.add_parser("clean", help="delete an edit's cached clip renders (keeps spec and final render)")
    p.add_argument("spec", help=SPEC_HELP)
    p.add_argument("--segments", action="store_true", help="also delete the shared source-segment cache")
    p.set_defaults(func=cmd_edit_clean)

    p = ed.add_parser("beats", help="the named moves (freeze-zoom, replay, impact, caption pop) and transition presets (whip, glitch, zoom-through) a lineup can ask for")
    p.set_defaults(func=cmd_edit_beats)

    p = ed.add_parser("motion", help="the motion graphics (Remotion) a spec can use: list them, or draw one frame to a transparent PNG")
    mo = p.add_subparsers(dest="motion_cmd", metavar="<action>", required=True)
    mo.add_parser("list", help="the compositions, their length, hit time and props, and whether Node and the packages are ready")
    q = mo.add_parser("still", help="one frame of a composition as a transparent PNG")
    q.add_argument("comp")
    q.add_argument("--frame", type=int, default=45, help="frame number (default 45; 60 per second)")
    q.add_argument("--props", help="a JSON file of props")
    q.add_argument("--dur", type=float, help="the graphic's length in seconds (default: the composition's)")
    q.add_argument("--hit", type=float, help="where it lands, in seconds (default: the composition's)")
    q.add_argument("--fps", type=float, default=60.0)
    q.add_argument("--out", help="output .png")
    p.set_defaults(func=cmd_edit_motion)

    p = ed.add_parser("versions", help="every render of an edit that can be played, and what changed between them")
    p.add_argument("name", help="edit name")
    p.add_argument("--changes", type=int, default=0, metavar="N", help="also print the first N changes of each version")
    p.set_defaults(func=cmd_edit_versions)

    p = ed.add_parser("review", help="timestamped comments on a render: list, add, resolve, reply (the review page in the dashboard writes the same file)")
    p.add_argument("name", help="edit name")
    rv = p.add_subparsers(dest="review_cmd", metavar="<action>", required=True)
    q = rv.add_parser("list", help="the open comments (--all: every comment)")
    q.add_argument("--all", action="store_true")
    q.add_argument("--on", help="only comments made on this render: preview, final or v<N>")
    q = rv.add_parser("add", help="add a comment at a time of a render")
    q.add_argument("--at", required=True, help="192.4, 3:12 or 1:03:12 into the render")
    q.add_argument("--kind", default="change", choices=list(editreview.KINDS))
    q.add_argument("--on", default="preview", help="the render it is about: preview (default), final or v<N>")
    q.add_argument("text")
    q = rv.add_parser("resolve", help="mark a comment done, saying how and in which version it was seen to be fixed")
    q.add_argument("id")
    q.add_argument("--how", help="what was changed")
    q.add_argument("--in", dest="version", default="preview", help="the render the fix was seen in: preview (default), final or v<N>")
    q.add_argument("--wontfix", action="store_true", help="close it without a change (say why in --how)")
    q = rv.add_parser("reopen", help="the fix did not work")
    q.add_argument("id")
    q = rv.add_parser("reply", help="answer a question, or explain a choice")
    q.add_argument("id")
    q.add_argument("text")
    q = rv.add_parser("delete", help="remove a comment")
    q.add_argument("id")
    q = rv.add_parser("taste", help="turn a comment into a standing taste rule (and close it)")
    q.add_argument("id")
    q.add_argument("--text", help="the rule, if it should say more than the comment does")
    p.set_defaults(func=cmd_edit_review)

    p = ed.add_parser("taste", help="the user's standing preferences for edits")
    p.add_argument("action", choices=["list", "add", "retire"])
    p.add_argument("text", nargs="?", help="the rule (add) or its id (retire)")
    p.add_argument("--said", help="the user's own words")
    p.add_argument("--edit", help="the edit that prompted it")
    p.add_argument("--all", action="store_true", help="include retired rules")
    p.set_defaults(func=cmd_edit_taste)
