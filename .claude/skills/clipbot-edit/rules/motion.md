# Motion: speed, camera, transitions

## Speed

- **A ramp has a shape.** Fast in, slow on the payoff, back out - four keys
  (`edittemplates._slowmo_keys`). A constant 0.5x over a whole clip is not an effect,
  it is a slow clip.
- **Do not ramp inside a cut-to-cut window shorter than the ramp.** The keys must sit
  inside `[in, out]`; the template drops the ramp when they would not (a ramp that
  runs off the footage freezes the last frame).
- **Slowed footage needs frames.** 60 fps source at 0.35x is clean; 30 fps source at
  0.35x is a slideshow without `interp: blend|mci`.
- Audio follows: `pitch` for impacts, `tempo` for voice, `mute` for a stylised silence.
  See audio.md for why a ramp's sound is one `atempo`, not many.

## Camera

- **Punch, don't pan.** A kill gets `1.0 -> 1.12` over 0.2 s (`out2`), a 0.55 s hold,
  an `inout2` return over 0.55 s. Profile `punch` sets the amount (punchy 1.12,
  balanced 1.10, restrained 1.06).
- **Move before the kill, not on it.** A slow push-in across the lead-up (up to
  35 % of the punch) gives the punch a running start and is motion the viewer feels
  without noticing. It is also what keeps a long approach from failing the pacing
  gate (rules: HARD-RULES 7).
- **Never zoom into a webcam or a HUD you cannot crop out.** Use `framing.crop` on the
  clip (fractions of the source) to remove chat/webcam before framing, and look at the
  storyboard after.
- **One pulse per kill, at most four per clip.** A fifth pulse in a run is noise; let
  the callouts carry the rest (`edittemplates.MAX_PUNCHES`).
- Camera keys rise in time. The engine rejects a spec that goes backwards; a kill that
  lands inside the previous kill's move rides on it.

## Transitions

- **Punchy = hard cuts.** A kill reel is a sequence of moments; a crossfade between
  two unrelated firefights tells the viewer nothing and costs a second encode (xfade
  assembly re-encodes; all-cuts assembles by `-c copy`).
- `balanced`/`restrained` use a 0.2/0.4 s `fade`. Use `fadeblack`/`dip` only to mark a
  chapter or time jump, `flash` for an impact. Anything fancier (`radial`, `pixelize`,
  `zoomin`) needs a reason the viewer can feel; otherwise it is decoration.
- An xfade overlap is capped at half of either clip, so a 1 s clip cannot take a 3 s
  fade; the timeline silently shortens it - check `inspect` if timing matters.

## Flash

- `flash` on a headshot or nade only (`profile.flash`), 0.12 s, amount ~0.45. A flash
  on every kill stops being a flash.
