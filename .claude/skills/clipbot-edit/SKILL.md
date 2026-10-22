---
name: clipbot-edit
description: How to cut a complete video edit with ClipBot's edit engine (`clipbot edit ...`) - compilations, highlight reels and montages from YouTube/Kick/file sources with speed ramps, camera moves, callouts, music, sound effects and loudness mastering. Use whenever the user asks for a compilation, montage, highlight reel, "best of", an edit, or to re-render/improve a video made with the edit engine; also when they give feedback on an edit ("too slow", "zoom less", "I hate that sound"). Read HARD-RULES.md before writing a spec and the user's taste file before choosing anything.
---

# ClipBot edit

You are the editor. The engine renders what the spec says, exactly, so every
creative decision is yours and is written down in `spec.json` where it can be
seen and changed. A good edit comes from the loop below, not from a clever first
spec: **render, look, listen (by measurement), fix, only then present.**

## Before you write anything

1. Read `HARD-RULES.md` (short; every rule is a failure that really happened).
2. Read the user's taste: `python -m clipbot edit taste list`. Taste outranks the
   defaults here. If the user just told you a preference, add it
   (`edit taste add "<rule>" --said "<their words>"`) *before* you apply it.
3. Read the rule file for the kind of edit: `rules/gaming.md` for kill/clutch
   montages, `rules/roundups.md` for a numbered variety roundup (a "Mish Mash"),
   plus `rules/hooks.md`, `rules/motion.md`, `rules/audio.md`, `rules/cutting.md`,
   `rules/framing.md` as needed.
4. Know what you actually have: which sources, at what resolution (Kick tops out
   at 720p60 - an upscaled 1080p master is not 1080p detail), whether the
   footage's data (kill times, chapters) is *measured* or *guessed*.

## The ladder (do not skip rungs)

```
data -> spec -> validate -> preview -> verify + look -> fix -> final -> verify -> deliver
```

1. **Data.** Times come from a measurement, never memory: `killfeed.json`
   (`clipbot killfeed scan --workspace W` for a new stream), a transcript, a
   highlight list. If you did not measure it, say so in the spec's `notes` and in
   your report. Never present a moment as a kill/clutch/laugh you have not seen
   evidence for.
2. **Spec.** `clipbot edit new <name> --killfeed W [W...]` writes a structurally
   correct first draft for kill montages (`--help` lists profile, order, slow-mo,
   music/SFX by library id or file). For anything else write the spec by hand to
   `work/_edits/<name>/spec.json`. Treat generated specs as drafts: open them,
   change what your judgement says.
3. **Validate.** `clipbot edit validate <name>` prints duration, chapters and the
   pacing verdict. Fix every `dead stretch` before rendering: the gate is cheap
   here and expensive after a render.
4. **Preview.** `clipbot edit render <name> --preview` (540p, seconds to a minute).
   Iterate on previews; render final once.
5. **Verify and look.** `clipbot edit verify <name> --preview`, then *open the
   artefacts*: `verify/preview/joins_NN.jpg` (a frame after every join - wrong crops, bad
   framing) and `moments_NN.jpg` (a frame 0.6 s after every marker: callout text,
   names, a callout covering the action) and
   `loudness.png` (a level plot with cuts and events marked - look for dips and
   pumping). `verify` passing is necessary, not sufficient: it cannot see taste.
6. **Final.** `clipbot edit render <name>`. Then `edit verify <name>` again and
   `edit deliver <name> --variants shorts,square` when the platform needs them.
7. **Report** with the numbers (length, size, LUFS, true peak, pacing), what you
   checked by eye, what you could not check, and any warning the engine printed.

## Commands

```
clipbot killfeed scan --workspace W [--player Teck] [--force]   # kills -> killfeed.json
clipbot killfeed list --workspace W
clipbot edit new NAME --killfeed W [W...] [--game G] [--profile punchy|balanced|restrained]
        [--order chapters|chronological|score] [--max-clips N] [--slowmo none|nade|special|all]
        [--music mus_ID|FILE] [--sfx-hit sfx_ID|FILE] [--sfx-nade ...] [--sfx-headshot ...] [--preset ...]
clipbot edit episode NAME LINEUP.json [--no-snap] [--no-match] [--force]   # a roundup: see rules/roundups.md
clipbot edit validate|inspect NAME
clipbot edit render NAME [--preview] [--force] [--no-master] [--preset shorts_1080x1920]
clipbot edit verify NAME [--preview] [--shallow]
clipbot edit deliver NAME [--variants shorts,square,portrait] [--thumbs N]
clipbot edit list | clean NAME [--segments] | taste list|add|retire
clipbot library list                       # music (mus_...), sfx (sfx_...), fonts, stickers
```

`NAME` is a folder under `work/_edits/`. Run everything from the repo root
(`D:\Claude\Code\ClipBot`). Long renders: run them in the background and read the
output; a 10-minute final at 1080p60 `slow` encodes at roughly realtime.

## Spec cheat sheet

Full schema and limits: `clipbot/editspec.py` and CLAUDE.md "The edit engine".

- **Positions are source seconds, durations are output seconds.** A kill at
  `t=4061.5` is `"at": 4061.5` in a marker, an overlay, a camera key, an SFX
  anchor - whatever a slow-mo ramp does to the clip. Never write "12 s into the
  video".
- Clip: `{id, src, in, out, label, tags, speed[], camera[], overlays[], markers[],
  fx[], grade, audio{slowmo: tempo|pitch|mute, gain_db}, transition_in, interp}`.
  Card: `{id, type: "card", dur, bg, overlays[]}`.
- `speed`: keys `{at, rate, ease}`. Fast in, slow on the payoff, back out:
  `[1.0 @ k-0.7, 0.35 @ k-0.15, 0.35 @ k+0.9, 1.0 @ k+1.5]`. `interp: blend`
  smooths slow-mo; `mci` is better and 10x slower.
- `camera`: keys `{at, zoom, ease}` rising in time. A punch is
  `1.0 @ k-0.2 -> 1.12 @ k -> hold -> 1.0 @ k+1.1`.
- `overlays`: `callout` (accent bar + icon + headline + sub), `text`, `image`.
  Hold 1-4 s (the pacing gate flags outside that). Never over the crosshair.
- `tracks.sfx[]`: `{id, asset|file, on: {clip, at|marker, offset}, gain_db, align:
  peak|start, lead}`; `tracks.music[]`: `{id, asset|file, gain_db, duck{...},
  fade_in, fade_out}`. Beds are ducked by the game audio.
- `master`: `-14 LUFS`, true peak `-1 dBTP`, limiter on, 0.6 s fade-out. Change
  only if the user asks for a different platform target.
- Tag a clip `chapter` (label = chapter title) to make it a chapter start;
  chapters are >= 10 s apart, 0:00 forced.

## After the user reacts

Feedback is data. Translate it into the spec, re-render a preview, then ask
whether it is a standing preference (`edit taste add`). If a *rule* turns out to be
wrong or missing, fix the rule file and cite the failure - this skill is only
worth anything if its rules keep recording what went wrong. See HARD-RULES.md
"How rules get added".

## Credit

The rules-from-failures structure, the verify-before-present gate, the pacing
gate, peak-aligned sound effects, the grade strengths and the delivery package are
adapted from github.com/ranahaani/i-hate-editing (MIT). The kill-feed reader and
the engine's own failures are this project's.
