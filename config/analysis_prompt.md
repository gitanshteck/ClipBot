<!--
Prompt template for stage 4 (Claude analysis). Placeholders substituted by
clipbot/stages/analyze.py:

    {{TRANSCRIPT}}   timestamped transcript lines
    {{RUBRIC}}       contents of the rubric file (config/rubric.md)
    {{DURATION}}     stream duration, human-readable
    {{STREAM_TITLE}} VOD title, if known

{{CACHE_BREAKPOINT}} marks where prompt caching splits the message. Everything
ABOVE it is cached; everything below is re-sent each call. The transcript sits
above and the rubric below on purpose: editing the rubric and re-running against
the same transcript then costs ~10% of the input price. Keep that order unless
you have a reason to change it.

Edit the wording freely; keep the placeholders and the JSON schema intact, since
the pipeline parses the response.
-->

Below is a timestamped transcript of a livestream VOD titled "{{STREAM_TITLE}}",
running {{DURATION}}.

Each line is `[start - end] text`, in seconds from the start of the stream.
Lines marked `(low-confidence)` came back with weak recognition scores — the
audio is Hindi with English code-switching, and background music and silence can
make the transcription model hallucinate plausible-looking text. Read flagged
lines as approximate, and don't build a clip on flagged text alone.

# Transcript

{{TRANSCRIPT}}

{{CACHE_BREAKPOINT}}

# Rubric

You are helping select candidate clips for a human editor to review. This rubric
defines what counts as clip-worthy on this channel.

{{RUBRIC}}

# Your task

Identify the moments that best match the rubric. For each one, return the time
range, a one-line description, and why it is clip-worthy.

Respond with **only** a JSON object, no prose or code fences, in exactly this shape:

```
{
  "clips": [
    {
      "start_time": 1234.5,
      "end_time": 1271.0,
      "description": "One line, plain and specific - what happens in this clip.",
      "why": "Which rubric signal it hits and what makes it land."
    }
  ]
}
```

Rules:
- `start_time` and `end_time` are seconds (numbers, not strings), and must fall
  within the transcript's range with `end_time > start_time`.
- Order clips by `start_time`.
- Ranges must not overlap; merge moments that run together.
- If nothing meets the rubric, return `{"clips": []}`.
