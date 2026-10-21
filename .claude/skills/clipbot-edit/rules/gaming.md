# Gaming montages (kill reels, clutch compilations)

Read with `HARD-RULES.md`. Wardogs numbers below were measured on the user's own
streams; another game needs its own measurement.

## What makes a kill reel worth watching

- **Context, then payoff, then a beat to breathe.** The default window is 6 s before
  and 3 s after a kill (`PROFILES.punchy`); a grenade gets +3 s before and +1 s after,
  because the throw is the story. A sniper kill needs the approach in frame: the
  viewer must see the shooter find the target to feel 238 m.
- **Order builds.** `--order chapters` runs HEADSHOTS -> 50 m+ -> 100 m+ -> MULTI-KILLS
  -> NADES, weakest to strongest inside each chapter. The end of the reel is the best
  thing in it. Plain kills (no headshot, < 50 m, alone) are left out of the chaptered
  cut on purpose; `--order chronological` keeps every kill when the user wants a log.
- **Rank by what the viewer reacts to:** grenade (+5) > headshot (+2) > through-cover
  (+1) > distance (100 m+ 4, 50 m+ 2.5, 25 m+ 1); a run of 3+ kills in 50 s adds its
  length. Tune `edittemplates.kill_score` rather than hand-reordering.
- **Never mix kills of different streams into one clip.** They are different clips
  with a cut between them; the generator never merges across sources.

## Callouts

- Headline = what happened (`HEADSHOT`, `NADE KILL`, `DOUBLE KILL`, `120 M SHOT`);
  sub line = `distance  victim`. The count builds across a clip: first kill plain,
  second `DOUBLE KILL`, third `TRIPLE KILL`.
- The weapon icon in the callout is the game's own, cropped from the feed
  (`killfeed_icons/`). It stops at the last modifier icon - the distance is drawn by
  the callout, not left in the crop (failure: `[55` digits showed beside "55 M").
- A name the OCR could not read is omitted, never guessed: noise on screen is worse
  than nothing (`killfeed.readable_name`, applied by `kills_from_killfeed` to every
  event, so data imported from another scan is filtered too). *Origin:* the first
  preview of the Teck reel put `(HUNT)MMRÅ+O`, `r--r` and `ExeCt;TtoNetZetÆ` in
  callouts because the imported blueprint names were never filtered; found by reading
  the kill-moment contact sheet, which `edit verify` now also writes
  (`verify/<tier>/moments_NN.jpg`: a frame 0.6 s after every marker) - open it
  before presenting a callout-heavy edit.

## Slow motion

- Only where the moment earns it: `--slowmo nade` (default) or `special` (nade,
  headshot, 100 m+). `all` makes every kill a slow-mo and none special.
- Shape: fast approach, slow payoff, snap back (`0.35x` on the kill for ~1 s). Keep
  `audio.slowmo: pitch` for impacts (a pitched-down shot sounds heavy); `tempo` for
  speech. `blend` interpolation is fine at 0.35x; `mci` only for a hero shot.

## Data hygiene

- **Distances are the mode of the frames a row was on screen.** A single OCR read is
  never trusted (`[` read as `1` made 10 m into 110 m). When quoting a distance to
  the user, say it was read from the feed and spot-check the extremes by eye
  (238 m, 99 m and 73 m were checked against frames before being used).
- **Deaths are not kills.** The player's name after the distance tag is a death.
- **A stream whose webcam overlay covers the feed returns nothing.** That means
  "unreadable", not "no kills" - tell the user which streams were skipped and why.
- YouTube keyframes are ~6 s apart; the reader decodes every frame so a short-lived row
  is not missed. Do not "optimise" the scan to keyframes.

## Sources

- Prefer the Kick VOD (on disk, exact) for footage when both exist; YouTube segments
  are fetched per clip and cost network time (a 30 s segment is seconds, not minutes).
- Check `source` fps: 60 fps footage in a 30 fps edit drops half the frames and makes
  slow-mo stutter. The engine warns; heed it.
