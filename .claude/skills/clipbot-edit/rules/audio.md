# Audio

## Targets

- **Programme loudness -14 LUFS integrated, true peak <= -1 dBTP** (YouTube's
  normalisation point; louder is turned down, quieter is not turned up). The engine
  measures the mix, applies the gain linearly (no pumping) and encodes AAC once.
- The verifier passes within +-1.5 LU. A peaky game mix lands a little under target
  because the limiter takes the peaks (-15.2 LUFS on the first Kick test at a -14
  target); say the measured number, do not claim -14.
- **Order of operations:** per-clip audio -> assemble -> music/SFX mix (beds ducked by
  the game audio) -> loudness measure -> linear normalise -> limiter -> fade-out -> AAC.
  Normalise *after* the mix, never per clip: per-clip normalisation makes a quiet
  moment as loud as a firefight.
- `normalize=0` on every `amix`. Without it ffmpeg halves each input as inputs are
  added and the SFX vanish.

## Sound effects

- **Align the peak, not the start.** `editaudio.find_peak` finds the loudest 5 ms; the
  SFX starts `peak_at` early so the energy lands on the kill, `lead` 0.05 s ahead of
  the picture (sound that arrives before the image feels tight; after feels late).
- An SFX that would start before the video is trimmed and the engine warns.
- Density: punchy 5 per 15 s, balanced 4, restrained 3 (`sfx-density` check). On a
  kill reel the game's own gunshot is already the sound effect; add one on the nade
  and the headshot, not on every kill.
- Never reuse the same SFX twice within a few seconds; pick another from
  `clipbot library list`.

## Music

- A bed at about -18 dB, `duck.enabled`, `fade_in` 0.5 s, `fade_out` 0.8 s. It loops.
- Sidechain: the game audio is the key; threshold 0.05, ratio 8, attack 20 ms,
  release 300 ms. If the music pumps audibly, raise `release`, not lower the ratio.
- Do not add music to a reel whose audio *is* the content without telling the user.
  Match tempo to cuts only if `detect_beats` returns a steady pulse (a bpm, not
  None); otherwise do not snap.
- Check the library licence (`clipbot library licenses`) before shipping music to a
  platform with content ID.

## Slow-motion sound

- **A ramp's sound is ONE input and ONE grouped `atempo`.** `atempo` loses ~25 ms
  per instance and `concat` pads the gap with silence; a chain per 0.1 s piece dips at
  every boundary. The source is trimmed 0.07 s long and cut back to the exact
  planned length so picture and sound end together.
- `atempo` accepts 0.5-2.0 per instance; `atempo_chain` chains for lower rates.
- `pitch` mode is an exact resample per piece (`asetrate`) - right for impacts, wrong
  for speech (it turns a voice into a monster).

## Joins

- **30 ms fade at every cut** removes the click a hard cut makes through a waveform that
  was not at a zero crossing. `verify` scores the sample jump at every join (limit 14)
  and warns with the join ids.
- A card's sound is silence; that is not a "silence" failure (no gap over 1.5 s outside
  planned cards is the check).
