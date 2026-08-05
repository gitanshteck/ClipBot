"""ClipBot CLI.

Full pipeline:      python -m clipbot run <kick-vod-url>
Individual stages:  python -m clipbot download <url>
                    python -m clipbot audio --workspace <slug>
                    ...

Every stage resolves its inputs through a Workspace, so any stage can be re-run
on its own against an existing workspace without repeating earlier ones.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import chatsync
from . import manifest as manifest_module
from . import review
from .config import Settings, load_settings
from .stages import analyze as analyze_stage
from .stages import audio as audio_stage
from .stages import chat as chat_stage
from .stages import cut as cut_stage
from .stages import download as download_stage
from .stages import diarize as diarize_stage
from .stages import reel as reel_stage
from .stages import transcribe as transcribe_stage
from .stages import transliterate as transliterate_stage
from .utils import (
    StageError,
    ToolMissingError,
    get_logger,
    human_size,
    setup_logging,
)
from .workspace import Workspace

log = get_logger("clipbot")



def resolve_workspace(args: argparse.Namespace, settings: Settings) -> Workspace:
    """Locate the workspace from --url or --workspace (slug or path)."""
    url = getattr(args, "url", None)
    if url:
        return Workspace.for_url(settings.work_root, url)

    name = getattr(args, "workspace", None)
    if not name:
        raise StageError("Pass either a VOD --url or --workspace <slug|path>.")

    candidate = Path(name)
    if not candidate.is_dir():
        candidate = settings.work_root / name
    return Workspace.open(candidate).ensure()


# --- commands -------------------------------------------------------------


def cmd_download(args: argparse.Namespace, settings: Settings) -> int:
    ws = Workspace.for_url(settings.work_root, args.url)
    video = download_stage.download_vod(args.url, ws, settings, force=args.force)
    print(video)
    return 0


def cmd_chat(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    path = chat_stage.fetch_chat(ws, settings, force=args.force)
    print(path)
    return 0


def cmd_chat_sync(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    chat_doc = chat_stage.load_chat(ws)
    result = chatsync.estimate_offset(ws, chat_doc, settings)
    print(json.dumps(result, indent=2))

    if args.apply:
        # Prefer the boundary heuristic when correlation says it isn't
        # confident - on sparse chat (this channel: 358 messages / 4.6h) the
        # correlation peak is measurably too weak to trust on its own.
        chosen = result["offset_seconds"]
        if not result["confident"] and result.get("boundary_offset_seconds") is not None:
            chosen = result["boundary_offset_seconds"]
        ws.update_state(chat_offset_seconds=chosen)
        print("\nApplied chat_offset_seconds = {0}".format(chosen))
    else:
        print("\nRun again with --apply to save this into state.json.")
    return 0


def cmd_audio(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    path = audio_stage.extract_audio(ws, settings, force=args.force)
    print(path)
    return 0


def cmd_transcribe(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)

    # Benchmark mode: transcribe only the first N seconds, to a side file, and
    # don't mark the stage done - this is for measuring throughput, not output.
    if args.max_seconds:
        clip = ws.root / "audio.bench.wav"
        audio_stage.trim_audio(
            ws.audio_path, clip, args.max_seconds, settings, start=args.start_seconds
        )
        out = ws.root / "transcript.bench.json"
        path = transcribe_stage.transcribe_audio(
            ws,
            settings,
            force=True,
            audio_path=clip,
            out_path=out,
            mark_stage=False,
        )
        data = ws.read_json(path)
        log.info(
            "BENCHMARK: %.2fx realtime -> a %.0f-minute stream would take ~%.0f minutes",
            data["realtime_factor"],
            60.0,
            60.0 / data["realtime_factor"] if data["realtime_factor"] else 0.0,
        )
        print(path)
        return 0

    path = transcribe_stage.transcribe_audio(ws, settings, force=args.force)
    print(path)
    return 0


def cmd_analyze(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    path = analyze_stage.analyze_transcript(ws, settings, force=args.force)
    # Fold the fresh candidates into review state, preserving any approvals and
    # hand-edited in/out points from a previous rubric.
    review.ensure_imported(ws)
    print(path)
    return 0


def cmd_transliterate(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    path = transliterate_stage.transliterate_transcript(ws, settings, force=args.force)
    print(path)
    return 0


def cmd_diarize(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    path = diarize_stage.diarize_audio(ws, settings, force=args.force)
    print(path)
    return 0


def cmd_cut(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    clips_dir = cut_stage.cut_clips(
        ws, settings, force=args.force, clip_ids=args.clip or None
    )
    manifest_module.write_manifest(ws, settings)
    print(clips_dir)
    return 0


def cmd_reel(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    out = reel_stage.render_reels(
        ws,
        settings,
        force=args.force,
        clip_ids=args.clip or None,
        preset=args.preset,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        manifest_module.write_manifest(ws, settings)
        print(out)
    return 0


def cmd_manifest(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    json_path, csv_path = manifest_module.write_manifest(ws, settings)
    print(json_path)
    print(csv_path)
    return 0


def cmd_cleanup(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)

    # Deleting the VOD is irreversible without re-downloading, so refuse while
    # approved clips are still uncut.
    pending = cut_stage.uncut_approved(ws)
    if pending and not args.force:
        raise StageError(
            "{0} approved clip(s) have not been cut yet - deleting the VOD now "
            "would mean re-downloading it to produce them.\n"
            "Run `clipbot cut` first, or pass --force to delete anyway.".format(
                len(pending)
            )
        )

    # Not a refusal: the chat fetch needs only the channel id and start time
    # from state.json, never the video file. But this is the last natural
    # prompt before the workspace looks "finished", and Kick's retention clock
    # is still running.
    if not ws.chat_path.exists():
        log.warning(
            "No chat.json in this workspace. Chat can still be fetched without "
            "the VOD file, but only until Kick expires it - run `clipbot chat "
            "--workspace %s` now if you want it.",
            ws.slug,
        )

    download_stage.delete_vod(ws)
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    ws = Workspace.for_url(settings.work_root, args.url)
    log.info("Workspace: %s", ws.root)

    video = download_stage.download_vod(args.url, ws, settings, force=args.force)
    # Chat first, and best-effort: Kick discards it with the VOD after 7 days
    # (30 if verified), so a later run may find nothing left to fetch. A stream
    # with no chat must not stop the rest of the pipeline.
    try:
        chat_stage.fetch_chat(ws, settings, force=args.force)
    except StageError as exc:
        log.warning("Chat unavailable, continuing without it: %s", exc)
    audio_stage.extract_audio(ws, settings, force=args.force, video=video)
    transcribe_stage.transcribe_audio(ws, settings, force=args.force)
    analyze_stage.analyze_transcript(ws, settings, force=args.force)
    review.ensure_imported(ws)

    if args.cut_all:
        cut_stage.cut_clips(ws, settings, force=args.force)
        manifest_module.write_manifest(ws, settings)
        log.info("Clips are in %s", ws.clips_dir)
    else:
        tally = review.counts(review.load(ws))
        log.info(
            "%d candidate(s) ready for review. Approve them, then run `clipbot cut "
            "--workspace %s` (or pass --cut-all to skip review).",
            tally["total"],
            ws.slug,
        )

    log.info(
        "The VOD was kept at %s - run `clipbot cleanup` once you're happy with the clips.",
        video.name,
    )
    return 0


def cmd_info(args: argparse.Namespace, settings: Settings) -> int:
    ws = resolve_workspace(args, settings)
    state = ws.read_state()

    print("workspace : {0}".format(ws.root))
    print("url       : {0}".format(state.get("url", "-")))
    print("title     : {0}".format(state.get("title", "-")))
    duration = state.get("duration")
    if duration:
        print("duration  : {0:.1f} min".format(duration / 60.0))

    video = ws.video_path()
    if video:
        print("video     : {0} ({1})".format(video.name, human_size(video.stat().st_size)))
    elif state.get("video_deleted"):
        print("video     : deleted (cleanup stage)")
    else:
        print("video     : -")

    for label, path in (
        ("audio", ws.audio_path),
        ("transcript", ws.transcript_path),
        ("candidates", ws.candidates_path),
        ("captions", ws.captions_path),
        ("manifest", ws.manifest_json_path),
    ):
        mark = human_size(path.stat().st_size) if path.exists() else "-"
        print("{0:<10}: {1}".format(label, mark))

    if ws.clips_path.exists():
        tally = review.counts(review.load(ws))
        print(
            "review    : {0} clip(s) - {1} approved, {2} cut, {3} pending, "
            "{4} rejected".format(
                tally["total"],
                tally["approved"],
                tally["cut"],
                tally["pending"],
                tally["rejected"],
            )
        )

    clips = sorted(ws.clips_dir.glob("*")) if ws.clips_dir.is_dir() else []
    print("clips     : {0} file(s)".format(len(clips)))

    stages = state.get("stages", {})
    print("stages    : {0}".format(", ".join(sorted(stages)) or "none"))
    return 0


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    root = settings.work_root
    if not root.is_dir():
        print("No workspaces yet ({0} does not exist).".format(root))
        return 0
    entries = sorted(p for p in root.iterdir() if p.is_dir())
    if not entries:
        print("No workspaces in {0}".format(root))
        return 0
    for path in entries:
        state = Workspace(path).read_state()
        print(
            "{0:<40} {1}".format(
                path.name, state.get("title") or state.get("url") or ""
            )
        )
    return 0


def cmd_library(args: argparse.Namespace, settings: Settings) -> int:
    """The cross-stream editing library. Deliberately takes no --workspace:
    sounds, stickers and effect presets belong to you, not to one VOD."""
    from . import library

    action = getattr(args, "library_command", None)
    root = library.library_root(settings)

    if action == "path":
        print(library.ensure_layout(root))
        return 0

    if action == "scan":
        index = library.scan(settings, prune=getattr(args, "prune", False))
        counts = index["counts"]
        print("{0}: {1}".format(
            root, ", ".join("{0} {1}".format(v, k) for k, v in sorted(counts.items()))
        ))
        for entry in index.get("errors") or []:
            print("  ! {0}: {1}".format(entry["rel"], entry["reason"]))
        for dupe, paths in (index.get("duplicates") or {}).items():
            print("  = duplicate content, ignored: {0}".format(", ".join(paths)))
        if index.get("missing"):
            print("  {0} asset(s) missing; `library list --missing` to see "
                  "them".format(len(index["missing"])))
        return 0

    if action == "list":
        index = library.load_index(settings)
        if getattr(args, "missing", False):
            for entry in index.get("missing") or []:
                print("{0:<24} {1}".format(entry["id"], entry.get("rel") or ""))
            return 0
        for asset in index.get("assets") or []:
            if args.kind and asset["kind"] != args.kind:
                continue
            if args.tag and args.tag not in (asset.get("tags") or []):
                continue
            detail = ""
            if asset.get("duration"):
                detail = "{0:.2f}s".format(asset["duration"])
            elif asset.get("width"):
                detail = "{0}x{1}{2}".format(
                    asset["width"], asset["height"],
                    " animated" if asset.get("animated") else "")
            print("{0:<24} {1:<8} {2:<28} {3:<12} {4}".format(
                asset["id"], asset["kind"], asset["name"][:28], detail,
                ",".join(asset.get("tags") or [])))
        return 0

    if action == "presets":
        for preset in library.list_presets(settings):
            print("{0:<20} {1:<28} {2} effect(s), anchor={3}".format(
                preset["id"], preset["name"][:28], len(preset["effects"]),
                preset["anchor"]))
        return 0

    if action == "speakers":
        # Creation/renaming is dashboard-primary (it involves picking an
        # avatar image), same asymmetry as presets - the CLI here is just
        # for scripting/inspection.
        for speaker in library.list_speakers(settings):
            print("{0:<20} {1:<24} avatar={2:<20} {3}".format(
                speaker["id"], speaker["name"][:24],
                speaker.get("avatar_asset") or "-", speaker.get("color") or ""))
        return 0

    if action == "licenses":
        index = library.load_index(settings)
        for asset in index.get("assets") or []:
            lic = asset.get("license") or {}
            print("{0:<24} {1:<24} {2:<12} {3}".format(
                asset["id"], asset["name"][:24],
                lic.get("name") or "unknown",
                lic.get("source") or ""))
        return 0

    if action == "hash":
        # The whole point of a manifest entry is that its sha256 is real, so
        # producing one is a single command rather than an exercise.
        path = Path(args.file)
        if not path.is_file():
            print("no such file: {0}".format(path))
            return 1
        entry = {
            "slug": library.slugify_name(path.stem),
            "kind": args.kind or "sfx",
            "url": "https://REPLACE-ME",
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": library._sha256(path),
            "name": path.stem,
            "tags": [],
            "license": {"name": "CC0-1.0", "author": "", "source": "", "url": ""},
        }
        print(json.dumps(entry, indent=2))
        return 0

    if action == "fetch":
        results = library.fetch_starter(
            settings,
            manifest_path=getattr(args, "manifest", None),
            only=getattr(args, "only", None),
            force=getattr(args, "force", False),
        )
        print("downloaded {0}, already present {1}, failed {2}".format(
            len(results["ok"]), len(results["skipped"]), len(results["failed"])))
        for failure in results["failed"]:
            print("  ! {0}: {1}".format(failure["slug"], failure["error"]))
        # Only a total failure is worth a non-zero exit: one dead CDN link
        # should not fail a run that fetched everything else.
        if results["failed"] and not results["ok"] and not results["skipped"]:
            return 1
        return 0

    return 1


# --- parser ---------------------------------------------------------------


def add_workspace_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="Kick VOD URL (derives the workspace name)")
    group.add_argument("--workspace", help="Existing workspace slug or path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipbot",
        description="Turn Kick livestream VODs into candidate clips.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--settings", help="path to a settings JSON file (default config/settings.json)"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_run = sub.add_parser("run", help="run the full pipeline for a VOD URL")
    p_run.add_argument("url")
    p_run.add_argument("--force", action="store_true", help="redo completed stages")
    p_run.add_argument(
        "--cut-all",
        action="store_true",
        help="cut every candidate without reviewing first",
    )
    p_run.set_defaults(func=cmd_run)

    p_dl = sub.add_parser("download", help="stage 1: download a VOD")
    p_dl.add_argument("url")
    p_dl.add_argument("--force", action="store_true", help="re-download if present")
    p_dl.set_defaults(func=cmd_download)

    p_chat = sub.add_parser(
        "chat", help="stage 1b: harvest the stream's chat (do this early - it expires)"
    )
    add_workspace_args(p_chat)
    p_chat.add_argument("--force", action="store_true", help="refetch if chat.json exists")
    p_chat.set_defaults(func=cmd_chat)

    p_sync = sub.add_parser(
        "chat-sync", help="estimate chat_offset_seconds by correlating chat against audio"
    )
    add_workspace_args(p_sync)
    p_sync.add_argument(
        "--apply", action="store_true", help="save the estimate into state.json"
    )
    p_sync.set_defaults(func=cmd_chat_sync)

    p_audio = sub.add_parser("audio", help="stage 2: extract the audio track")
    add_workspace_args(p_audio)
    p_audio.add_argument("--force", action="store_true", help="re-extract if present")
    p_audio.set_defaults(func=cmd_audio)

    p_tr = sub.add_parser("transcribe", help="stage 3: transcribe audio with faster-whisper")
    add_workspace_args(p_tr)
    p_tr.add_argument("--force", action="store_true", help="re-transcribe if present")
    p_tr.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        metavar="N",
        help="benchmark mode: transcribe only N seconds to transcript.bench.json",
    )
    p_tr.add_argument(
        "--start-seconds",
        type=float,
        default=0.0,
        metavar="N",
        help="with --max-seconds: offset to start the benchmark slice at "
        "(a stream's opening is usually a music-only waiting screen)",
    )
    p_tr.set_defaults(func=cmd_transcribe)

    p_an = sub.add_parser("analyze", help="stage 4: ask Claude for clip candidates")
    add_workspace_args(p_an)
    p_an.add_argument(
        "--force",
        action="store_true",
        help="re-run against the existing transcript (use after editing the rubric)",
    )
    p_an.set_defaults(func=cmd_analyze)

    p_tl = sub.add_parser(
        "transliterate",
        help="stage 3c: convert the Devanagari transcript to Hinglish captions",
    )
    add_workspace_args(p_tl)
    p_tl.add_argument(
        "--force", action="store_true", help="re-run against the existing transcript"
    )
    p_tl.set_defaults(func=cmd_transliterate)

    p_dz = sub.add_parser(
        "diarize",
        help="opt-in: speaker diarization (needs "
        "requirements-diarize.txt + a Hugging Face token)",
    )
    add_workspace_args(p_dz)
    p_dz.add_argument("--force", action="store_true", help="re-run against the existing audio")
    p_dz.set_defaults(func=cmd_diarize)

    p_cut = sub.add_parser("cut", help="stage 5: cut approved clips out of the VOD")
    add_workspace_args(p_cut)
    p_cut.add_argument(
        "--force", action="store_true", help="re-cut clips whose files already exist"
    )
    p_cut.add_argument(
        "--clip",
        action="append",
        metavar="ID",
        help="cut only this clip id (repeatable); default is every approved clip",
    )
    p_cut.set_defaults(func=cmd_cut)

    p_reel = sub.add_parser(
        "reel", help="stage 5b: export approved clips as 9:16 vertical reels"
    )
    add_workspace_args(p_reel)
    p_reel.add_argument(
        "--clip", action="append", metavar="ID",
        help="render only this clip id (repeatable); default is every approved clip",
    )
    p_reel.add_argument(
        "--preset",
        choices=["cam_top", "cam_bottom", "blur_fill", "pip", "game_only"],
        help="override the layout for this run",
    )
    p_reel.add_argument(
        "--force", action="store_true", help="re-render even if the file is current"
    )
    p_reel.add_argument(
        "--dry-run", action="store_true",
        help="print the ffmpeg command for each clip and render nothing",
    )
    p_reel.set_defaults(func=cmd_reel)

    p_man = sub.add_parser("manifest", help="write manifest.json + manifest.csv")
    add_workspace_args(p_man)
    p_man.set_defaults(func=cmd_manifest)

    p_clean = sub.add_parser("cleanup", help="stage 6: delete the downloaded VOD")
    add_workspace_args(p_clean)
    p_clean.add_argument(
        "--force",
        action="store_true",
        help="delete even if approved clips have not been cut yet",
    )
    p_clean.set_defaults(func=cmd_cleanup)

    p_info = sub.add_parser("info", help="show workspace status")
    add_workspace_args(p_info)
    p_info.set_defaults(func=cmd_info)

    p_list = sub.add_parser("list", help="list known workspaces")
    p_list.set_defaults(func=cmd_list)

    p_lib = sub.add_parser(
        "library",
        help="manage the cross-stream effects library (sounds, stickers, presets)",
    )
    p_lib.set_defaults(func=cmd_library)
    # dest= and required= must both be spelled out: on Python 3.9 a nested
    # subparser without them lets a bare `clipbot library` through, which then
    # dies with an AttributeError instead of printing this help.
    lib_sub = p_lib.add_subparsers(dest="library_command", metavar="<action>",
                                   required=True)

    p_lib_scan = lib_sub.add_parser("scan", help="re-read the drop-in folders")
    p_lib_scan.add_argument(
        "--prune", action="store_true",
        help="forget deleted assets immediately instead of after 30 days",
    )

    p_lib_list = lib_sub.add_parser("list", help="list library assets")
    p_lib_list.add_argument("--kind", choices=sorted(("sfx", "music", "sticker",
                                                      "font")))
    p_lib_list.add_argument("--tag")
    p_lib_list.add_argument("--missing", action="store_true",
                            help="show assets whose file has gone")

    p_lib_fetch = lib_sub.add_parser(
        "fetch", help="download the CC0 starter pack named in the manifest")
    p_lib_fetch.add_argument("--manifest", help="override the manifest path")
    p_lib_fetch.add_argument("--only", nargs="+", metavar="SLUG",
                             help="fetch just these entries")
    p_lib_fetch.add_argument("--force", action="store_true",
                             help="re-download even if the file is already there")

    p_lib_hash = lib_sub.add_parser(
        "hash", help="print a ready-to-paste manifest entry for a local file")
    p_lib_hash.add_argument("file")
    p_lib_hash.add_argument("--kind", choices=sorted(("sfx", "music", "sticker",
                                                      "font")))

    lib_sub.add_parser("licenses", help="print where every asset came from")
    lib_sub.add_parser("presets", help="list saved effect chains")
    lib_sub.add_parser("speakers", help="list speaker profiles")
    lib_sub.add_parser("path", help="print the library folder (creating it)")

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    setup_logging(args.verbose)
    try:
        settings = load_settings(Path(args.settings) if args.settings else None)
        return args.func(args, settings)
    except (StageError, ToolMissingError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
