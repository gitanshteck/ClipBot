# Cutting and structure

- **Cut on the action, not the clock.** In/out come from the moment's own data (kill
  time minus pre-roll), not round numbers. A cut that lands mid-word or mid-recoil
  feels wrong even when the viewer cannot say why.
- **Pre-roll gives the setup, post-roll the reaction.** punchy 6 s / 3 s, balanced
  7 / 4, restrained 8 / 5. Shorter than 2 s of setup and the payoff has no weight;
  longer than the gap allowance with nothing happening and the pacing gate fails.
- **Merge close moments into one clip** (kills within 12 s). Two clips cut 4 s apart
  look like a mistake; one clip with two callouts looks like a play.
- **Build to the end.** The strongest moment goes last; the second strongest goes
  first or second (the hook); the middle is where the weakest belongs, or is cut.
- **Cut the weakest, not the most recent.** If the edit is too long, `--max-clips N`
  keeps the best by score and re-orders them; do not trim every clip equally.
- **Length follows the material.** 24 good kills is 4-5 minutes; padding it to 10 with
  ordinary kills makes the 24 worse. A Shorts cut is <= 180 s: choose its moments, do
  not squeeze the long cut.
- **Chapters** are clips tagged `chapter` (label = the chapter name) and cards.
  YouTube needs 0:00, 3 or more, each >= 10 s; `edit validate` reports eligibility.
  Cards are chapter starts unless tagged `nochapter`.
- **Cards are punctuation.** An intro card (2 s) names the player, game and counts; an
  outro (2.4 s) closes. Both are optional (`--no-intro`, `--no-outro`); a montage that
  is the opening of a longer video wants neither.
- **Fewer, longer sources beat many short ones for cost.** A YouTube segment is a
  network fetch; 60 clips from one stream cache together, 60 streams do not.
- **Do not trust round-trips of times through the UI.** Times in a spec are source
  seconds with millisecond precision; `inspect` shows what was resolved.
