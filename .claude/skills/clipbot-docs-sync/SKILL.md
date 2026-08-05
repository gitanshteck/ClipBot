---
name: clipbot-docs-sync
description: Keeps CLAUDE.md (the technical architecture map for this project) in sync with the actual ClipBot code. Use whenever a change touches anything CLAUDE.md documents — a pipeline stage, a server route, a job kind, a data schema (state.json/candidates.json/clips.json/chat.json/transcript.json/index.json), a settings key or default, a constant/threshold, a CLI subcommand, or a new module — and whenever the user asks to "update CLAUDE.md", "sync the docs", "refresh the docs", or similar. Also run a full verification pass at the start of a session if CLAUDE.md's "Last verified" date looks stale relative to work about to happen.
---

# ClipBot docs sync

This project has one always-loaded technical doc, `CLAUDE.md` at the repo
root. It exists so a coding agent doesn't have to re-derive ClipBot's
architecture from scratch every session. It is only useful if it stays
**accurate** — a wrong doc is worse than no doc, because it's trusted by
default. Your job when this skill runs is to make CLAUDE.md match the code,
not to make the code match CLAUDE.md.

There is no git repository here (confirmed: this project is not under git),
so there is no `git diff`/`git log` to lean on. Verification has to be done
by reading the actual source and comparing it against what CLAUDE.md
currently claims.

## Ground truth rules

1. **The code in `clipbot/` is ground truth.** `config/settings.json` is
   ground truth for defaults. `clipbot/cli.py`'s `build_parser()` is ground
   truth for what CLI commands exist. `clipbot/server/app.py`'s route
   decorators are ground truth for what HTTP routes exist.
2. **`README.md` is not ground truth — it drifts.** CLAUDE.md was written
   after discovering the README's top status banner was stale (claimed
   stages 3–6 were unbuilt when they were fully implemented). Don't
   "reconcile" CLAUDE.md toward the README if they disagree; trust the code.
   If you notice the README itself has drifted further, mention it to the
   user, but fixing README.md is not this skill's job unless asked.
3. **Never describe a function, route, field, or constant you have not just
   read.** If CLAUDE.md names something specific (a function signature, a
   JSON field, a default value, a threshold), grep or read the real file
   before repeating or updating that claim. A memory/doc claiming something
   exists is not the same as it existing now.

## When to do a targeted sync (most common case)

You just changed code in this session and the change affects a fact
CLAUDE.md states. Examples: added/removed a CLI subcommand, added/changed a
server route, changed a settings default, added a new effect type or reel
preset, changed a data shape (a new field on `clips.json`, a renamed key in
`transcript.json`, etc.), added a new pipeline stage or job kind, changed a
threshold or magic number that CLAUDE.md quotes verbatim.

Process:

1. Find the relevant section(s) in `CLAUDE.md` — it's organized by module
   (stage-by-stage walkthrough, spec modules, library system, dashboard,
   configuration reference, the "Quick index" at the bottom, and the
   "Cross-cutting invariants" list). A single change often touches two or
   three of these (e.g. a new effect type touches the fxspec section, the
   settings reference, the testing section, and the quick index).
2. Re-read the actual current source for what changed — not just the diff
   you made, the whole function/section, since the doc describes behavior
   and invariants, not line-by-line code.
3. Edit CLAUDE.md with the `Edit` tool, surgically — change only the
   sentences/bullets that are now wrong or newly relevant. Don't rewrite
   whole sections from scratch; that's how style drift and accidental fact
   loss happen.
4. Match the existing voice: dense, technical, bullet-heavy, no marketing
   language, no restating what a well-named identifier already says. Every
   non-obvious design decision should carry a one-clause "why" if you know
   it (check comments/docstrings in the source — this codebase documents
   its reasoning heavily; use that, don't invent a rationale).
5. If you added a genuinely new subsystem (a new top-level module, a new
   stage), give it a subsection matching the depth of existing ones: what
   it does, its main entry-point function signature, its data shape if it
   produces one, and any gotchas found in its comments — and add one line
   to the repository map tree and, if relevant, the pipeline stage table.
6. Update the `Last verified against the code:` date near the top of
   CLAUDE.md to today's date.
7. Spot-check: grep for 2-3 of the specific facts you just wrote (a
   function name, a constant, a route path) to confirm they exist verbatim
   in the source you just edited the doc to describe.

## When to do a full verification pass

Triggered by an explicit ask ("sync CLAUDE.md", "check the docs are still
accurate") or when you notice the "Last verified" date is old relative to
how much has changed. This is more expensive — do it deliberately, not on
every turn.

1. Read CLAUDE.md fully.
2. For each major section, verify its concrete claims against source:
   - **Repository map / pipeline table**: does `clipbot/cli.py`'s
     `build_parser()` still register exactly these subcommands? Does
     `clipbot/stages/` still contain exactly these files?
   - **Per-stage sections**: do the named functions
     (`download_vod`, `extract_audio`, `transcribe_audio`,
     `analyze_transcript`, `cut_clips`, `render_reels`, ...) still exist
     with roughly these signatures? Do the documented JSON shapes still
     match what the code actually writes (grep the dict literal or
     `write_json`/`mark_stage` calls)?
   - **Spec modules**: do `reelspec.PRESETS`/`LAYOUTS`/`CHAT_MODES` and
     `fxspec.TIMED_TYPES`/`WHOLE_TYPES`/`SINGLETON_TYPES`/`MAX_EFFECTS`
     still match?
   - **Configuration reference**: open `config/settings.json` and diff its
     actual current values against every specific number/string CLAUDE.md
     quotes (thresholds, defaults, model names, canvas sizes, etc.).
   - **Server routes**: does `clipbot/server/app.py` still expose the same
     route groups? Any new routes worth a one-line mention?
   - **"Known loose end" callouts** (e.g. the unregistered `waveform` job
     kind): check whether they've been fixed — if so, remove the callout
     and, if it became a real feature, document it properly instead.
   - **Cross-cutting invariants list**: for each invariant, find the
     assertion or comment in code that still backs it up; if the invariant
     was removed or changed, update or drop the line.
3. This is exactly the kind of broad, multi-file, read-only reconnaissance
   the `Explore` subagent is good at — for a full pass, consider spawning
   one Explore agent per cluster (stages, spec modules + library, server)
   with a prompt asking it to report **discrepancies against the current
   CLAUDE.md text** (paste the relevant section into the prompt) rather
   than re-deriving everything from zero, then apply the edits yourself.
4. Apply edits, update the "Last verified" date, and give the user a short
   summary of what changed (or "no drift found" if nothing did).

## What not to do

- Don't add speculative or aspirational documentation ("this could be used
  to..."). CLAUDE.md documents what exists, not roadmap ideas.
- Don't let the file grow unbounded — if a section is being restated with
  more and more caveats over time, that's a signal to tighten the prose,
  not just append.
- Don't copy README.md content into CLAUDE.md or vice versa — they serve
  different readers (README: a human setting the project up and tuning it;
  CLAUDE.md: an agent that needs the internals). Some facts legitimately
  live in both, worded differently for their audience.
- Don't touch `config/settings.json`, `config/rubric.md`, or any pipeline
  code as part of a "docs sync" — this skill only edits `CLAUDE.md` (and,
  if asked, this `SKILL.md` file itself). If you notice an actual bug while
  verifying (not just stale docs), tell the user rather than silently
  fixing it inline.
