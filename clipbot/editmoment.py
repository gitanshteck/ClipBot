"""The moment engine: build whole moments from kills, rank them, and show them before they are chosen.

An episode used to be assembled from `candidates.json` descriptions and transcript lines: Claude's
reading of a noisy Hindi transcript. That finds the *talk about* a moment, not the moment: a clip began at
the reaction ("it fell on me") instead of the hit, a kill came with six seconds before it and three
after, and the pool was whatever happened to be scanned. This module works from what happened on screen.

* **Arcs** (`arcs_from_kills`): every kill the kill-feed reader found (`killfeed.json`: the feed row, and
  the "KILL CONFIRMED" popup that survives a webcam over the feed) is a seed; kills close together are
  one arc (a multi-kill is one moment, not three); an arc is the kills plus a *lead-up* before the first
  and an *aftermath* after the last, then its edges are moved into the nearest pause in the speech
  (`editcut.find_cut`) so neither lands in a word.
* **Rank** (`score_arc`): what a viewer would care about, as parts that are printed with the number -
  the kills (`edittemplates.kill_score`: nade, headshot, distance), how many, a loudness peak in the arc
  (the streamer's reaction), a chat burst, and whether the rubric analysis picked the same stretch.
* **Evidence** (`evidence_sheet`): a contact sheet per arc - a dozen frames across it with the kills
  marked, the transcript lines, the numbers - so a moment is chosen from what is on screen.

What it does NOT do, measured rather than assumed: it does not find "the first gunfire" from the audio.
On 70 known kills the rate of sharp high-frequency onsets before a kill was the same as at random times
(0.34 vs 0.35 a second; the stream's mix is dominated by the voice), so the lead-up is a *policy*
(`LEAD`, `TAIL`) that the evidence sheet is there to correct by eye, not a detection.

Pure functions take plain dicts; `build` / `evidence_sheet` do the IO.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import editcut, edittemplates, highlights
from .config import Settings
from .utils import StageError, get_logger
from .workspace import MODE_EMBED, Workspace

log = get_logger(__name__)

SCHEMA = 1
LEAD = 14.0          # seconds of lead-up before the first kill of an arc
TAIL = 7.0           # seconds of aftermath after the last
MERGE_GAP = 16.0     # kills closer than this are one arc
MAX_ARC = 90.0       # an arc longer than this is split at its largest gap between kills

# the parts of an arc's score, and how much each can add
W_MULTI = 1.5        # per kill beyond the first
W_REACTION = 0.7     # per unit of loudness z-score over the stream's own spikes, capped
CAP_REACTION = 3.0
W_CHAT = 0.4         # per x of chat-rate multiplier, capped
CAP_CHAT = 2.0
W_ANALYSIS = 2.0     # the rubric analysis picked an overlapping stretch

Arc = Dict[str, Any]


# --------------------------------------------------------------------------
# arcs
# --------------------------------------------------------------------------


def _kill(e: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": e.get("id"), "t": float(e["t"]), "dist_m": e.get("dist_m"), "weapon": e.get("weapon") or "unknown",
            "headshot": bool(e.get("headshot")), "through_cover": bool(e.get("through_cover")),
            "victim": e.get("victim") or "", "source": e.get("source") or "feed"}


def _cluster(kills: List[Dict[str, Any]], merge_gap: float, max_arc: float, lead: float, tail: float) -> List[List[Dict[str, Any]]]:
    """Kills by time into groups: a gap over `merge_gap` starts a new one, and a group whose arc would be
    longer than `max_arc` is split at its largest gap."""
    groups: List[List[Dict[str, Any]]] = []
    for k in sorted(kills, key=lambda k: k["t"]):
        if groups and k["t"] - groups[-1][-1]["t"] <= merge_gap:
            groups[-1].append(k)
        else:
            groups.append([k])

    def split(g: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        if len(g) < 2 or g[-1]["t"] - g[0]["t"] + lead + tail <= max_arc:
            return [g]
        i = max(range(1, len(g)), key=lambda j: g[j]["t"] - g[j - 1]["t"])
        return split(g[:i]) + split(g[i:])

    return [part for g in groups for part in split(g)]


def arcs_from_kills(events: Sequence[Dict[str, Any]], duration: Optional[float], lead: float = LEAD, tail: float = TAIL,
                    merge_gap: float = MERGE_GAP, max_arc: float = MAX_ARC) -> List[Arc]:
    """Kill events (as in `killfeed.json`) -> arcs `{id, start, end, first, last, kills}`, in time order.

    The window is `lead` seconds before the first kill to `tail` after the last, clamped to [0, duration];
    two arcs whose windows touch are merged into one (so no footage is cut twice).
    """
    kills = [_kill(e) for e in events]
    arcs: List[Arc] = []
    for g in _cluster(kills, merge_gap, max_arc, lead, tail):
        start = max(0.0, g[0]["t"] - lead)
        end = g[-1]["t"] + tail
        if duration:
            end = min(end, float(duration))
        arcs.append({"start": round(start, 2), "end": round(end, 2), "first": g[0]["t"], "last": g[-1]["t"], "kills": g})
    merged: List[Arc] = []
    for a in arcs:
        if merged and a["start"] <= merged[-1]["end"]:
            m = merged[-1]
            if max(m["end"], a["end"]) - m["start"] <= max_arc:
                m["end"] = max(m["end"], a["end"])
                m["last"] = a["last"]
                m["kills"] += a["kills"]
                continue
            # touching but too long to be one: the boundary goes halfway between the two kills, so the
            # lead-up of one is never the aftermath of the other cut twice
            mid = (m["last"] + a["first"]) / 2.0
            m["end"] = round(min(m["end"], mid), 2)
            a["start"] = round(max(a["start"], mid), 2)
        merged.append(a)
    for n, a in enumerate(merged, start=1):
        a["id"] = "m{0:03d}".format(n)
    return merged


def context(arc: Arc) -> Dict[str, float]:
    """What an arc gives the viewer around its kills: seconds of lead-up, of aftermath, and its length."""
    return {"lead": round(arc["first"] - arc["start"], 2), "tail": round(arc["end"] - arc["last"], 2),
            "length": round(arc["end"] - arc["start"], 2)}


# --------------------------------------------------------------------------
# rank
# --------------------------------------------------------------------------


def _overlap(spans: Sequence[Dict[str, Any]], a: float, b: float) -> List[Dict[str, Any]]:
    return [s for s in spans if s["start"] < b and s["end"] > a]


def score_arc(arc: Arc, energy: Sequence[Dict[str, Any]] = (), chat: Sequence[Dict[str, Any]] = (),
              candidates: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """`{score, parts{...}, analysis}` for an arc. Parts are separate so a ranking can be argued with:
    `kills` (sum of `edittemplates.kill_score`), `multi`, `reaction`, `chat`, `analysis`."""
    kills = sum(edittemplates.kill_score(k) for k in arc["kills"])
    multi = W_MULTI * (len(arc["kills"]) - 1)
    spikes = _overlap(energy, arc["start"], arc["end"])
    reaction = min(CAP_REACTION, W_REACTION * max((s["z_score"] for s in spikes), default=0.0))
    bursts = _overlap(chat, arc["start"], arc["end"])
    chat_part = min(CAP_CHAT, W_CHAT * max((s.get("message_rate_multiplier", 0.0) for s in bursts), default=0.0))
    picked = [c for c in candidates if c["start_time"] < arc["end"] and c["end_time"] > arc["start"]]
    analysis = W_ANALYSIS if picked else 0.0
    parts = {"kills": round(kills, 2), "multi": round(multi, 2), "reaction": round(reaction, 2), "chat": round(chat_part, 2),
             "analysis": analysis}
    out: Dict[str, Any] = {"score": round(sum(parts.values()), 2), "parts": parts}
    if picked:
        out["analysis"] = [{"start": c["start_time"], "end": c["end_time"], "description": c.get("description", "")} for c in picked]
    return out


def range_arc(ws: Workspace, start: float, end: float, label: Optional[str] = None) -> Arc:
    """An arc for a stretch the author picked by hand (a clip already in a lineup, a candidate from the
    analysis): no kills, no score, but the same sheet - so a clip can be looked at *with its surroundings*
    before it is trusted. Pass the window you want to see, padding included."""
    transcript = ws.read_json(ws.transcript_path) if ws.transcript_path.exists() else {"segments": []}
    arc: Arc = {"id": label or "r{0:.0f}-{1:.0f}".format(start, end), "start": round(start, 2), "end": round(end, 2),
                "first": round(start, 2), "last": round(end, 2), "kills": [], "parts": {}, "score": None}
    arc["context"] = context(arc)
    arc["lines"] = lines_between(transcript, start, end, limit=24)
    return arc


def lines_between(transcript: Dict[str, Any], a: float, b: float, limit: int = 12) -> List[Dict[str, Any]]:
    """Transcript lines inside [a, b]: `{t, text}`, low-confidence ones kept and marked (never dropped)."""
    out = []
    for s in transcript.get("segments", []):
        if s["end"] > a and s["start"] < b and s.get("text", "").strip() not in ("", "?"):
            out.append({"t": round(s["start"], 1), "text": s["text"].strip(), "low": bool(s.get("low_confidence"))})
    return out[:limit]


# --------------------------------------------------------------------------
# build (IO)
# --------------------------------------------------------------------------


def moments_path(ws: Workspace) -> Path:
    return ws.root / "moments.json"


def load(ws: Workspace) -> Optional[Dict[str, Any]]:
    p = moments_path(ws)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _snap(arc: Arc, wav: Optional[Path]) -> None:
    """Move the arc's edges into a pause in the speech where there is one within 0.8 s (`editcut`); record
    whether each moved. The lead-up never gets shorter than the kill-free stretch it started with by more
    than the window, so the policy's seconds stay about what they were."""
    arc["snapped"] = {"in": False, "out": False}
    if wav is None or not wav.exists():
        return
    for kind, key in (("in", "start"), ("out", "end")):
        try:
            cut = editcut.find_cut(wav, arc[key], kind)
        except StageError:
            continue
        if cut["snapped"]:
            arc[key] = round(cut["t"], 2)
            arc["snapped"][kind] = True


def build(ws: Workspace, settings: Settings, lead: Optional[float] = None, tail: Optional[float] = None,
          merge_gap: Optional[float] = None, snap: bool = True) -> Dict[str, Any]:
    """Build and save `moments.json` for a workspace that has a `killfeed.json`. Returns the document."""
    kf_path = ws.root / "killfeed.json"
    if not kf_path.exists():
        raise StageError("{0} has no killfeed.json; run: clipbot killfeed scan --workspace {0}".format(ws.slug))
    kf = json.loads(kf_path.read_text(encoding="utf-8"))
    state = ws.read_state()
    duration = state.get("duration") or kf.get("duration")
    p = {"lead": float(lead if lead is not None else settings.get("moments.lead", LEAD)),
         "tail": float(tail if tail is not None else settings.get("moments.tail", TAIL)),
         "merge_gap": float(merge_gap if merge_gap is not None else settings.get("moments.merge_gap", MERGE_GAP))}
    arcs = arcs_from_kills(kf.get("events", []), duration, p["lead"], p["tail"], p["merge_gap"])

    energy: List[Dict[str, Any]] = []
    if ws.audio_path.exists():
        try:
            energy = highlights.compute_energy_spikes(ws.audio_path, float(settings.get("analyze.signals.audio_bin_seconds", 5.0)),
                                                      float(settings.get("analyze.signals.audio_z_threshold", 2.0)))
        except Exception as exc:  # noqa: BLE001 - a missing signal lowers a score, it does not stop the build
            log.warning("energy spikes skipped for %s: %s", ws.slug, exc)
    chat: List[Dict[str, Any]] = []
    if duration and ws.chat_path.exists():
        try:
            cdoc = ws.read_json(ws.chat_path)
            if cdoc.get("status") == "ok":
                chat = highlights.compute_chat_spikes(cdoc, float(state.get("chat_offset_seconds") or 0.0), float(duration))
        except Exception as exc:  # noqa: BLE001
            log.warning("chat spikes skipped for %s: %s", ws.slug, exc)
    candidates = ws.read_json(ws.candidates_path).get("clips", []) if ws.candidates_path.exists() else []
    transcript = ws.read_json(ws.transcript_path) if ws.transcript_path.exists() else {"segments": []}

    for a in arcs:
        if snap:
            _snap(a, ws.audio_path if ws.audio_path.exists() else None)
        a.update(score_arc(a, energy, chat, candidates))
        a["context"] = context(a)
        a["lines"] = lines_between(transcript, a["start"], a["end"])
    arcs.sort(key=lambda a: a["start"])

    by_source: Dict[str, int] = {}
    for e in kf.get("events", []):
        by_source[e.get("source") or "feed"] = by_source.get(e.get("source") or "feed", 0) + 1
    doc = {"schema": SCHEMA, "workspace": ws.slug, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "params": p,
           "duration": duration, "kills": len(kf.get("events", [])), "kills_by_source": by_source,
           "signals": {"energy_spikes": len(energy), "chat_spikes": len(chat), "candidates": len(candidates)}, "arcs": arcs}
    ws.write_json(moments_path(ws), doc)
    log.info("Moments: %d arc(s) from %d kill(s) in %s", len(arcs), doc["kills"], ws.slug)
    return doc


def ranked(doc: Dict[str, Any]) -> List[Arc]:
    return sorted(doc["arcs"], key=lambda a: (-a["score"], a["start"]))


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------


def coverage(ws: Workspace) -> Dict[str, Any]:
    """How much of a workspace's kill record is trustworthy: scanned or not, how many kills, from which
    source, and a verdict. Not a count of kills that happened (that would need the end-of-match scoreboard,
    which is not read): a stream with hours of play and no kills is flagged as "unread", not "no kills"."""
    state = ws.read_state()
    hours = float(state.get("duration") or 0.0) / 3600.0
    kf_path = ws.root / "killfeed.json"
    row: Dict[str, Any] = {"workspace": ws.slug, "title": state.get("title") or "", "hours": round(hours, 2),
                           "platform": state.get("platform", "kick"), "scanned": kf_path.exists()}
    if not kf_path.exists():
        row["verdict"] = "not scanned"
        return row
    kf = json.loads(kf_path.read_text(encoding="utf-8"))
    ev = kf.get("events", [])
    src = {"both": 0, "feed": 0, "confirm": 0}
    for e in ev:
        src[e.get("source") or "feed"] = src.get(e.get("source") or "feed", 0) + 1
    row.update({"kills": len(ev), "sources": src, "has_popup_pass": "confirm_sightings" in kf,
                "per_hour": round(len(ev) / hours, 1) if hours else None})
    if "confirm_sightings" not in kf:
        row["verdict"] = "feed only (rescan for the popup pass)" if ev else "feed only, nothing found (the feed may be covered; rescan)"
    elif not ev:
        row["verdict"] = "unread or no kills" if hours >= 1.0 else "no kills"
    elif src["confirm"] > src["both"]:
        row["verdict"] = "feed mostly covered: kills known, details missing"
    else:
        row["verdict"] = "ok"
    return row


# --------------------------------------------------------------------------
# evidence sheets
# --------------------------------------------------------------------------


def _devanagari(text: str) -> bool:
    """Pillow without libraqm cannot shape Devanagari (it draws boxes), so such a line is left off the
    picture and printed as text by `clipbot moments show` instead."""
    return any("\u0900" <= ch <= "\u097f" for ch in text)


def _mmss(t: float) -> str:
    t = int(round(t))
    return "{0}:{1:02d}:{2:02d}".format(t // 3600, (t // 60) % 60, t % 60) if t >= 3600 else "{0}:{1:02d}".format(t // 60, t % 60)


def frame_times(arc: Arc, n: int = 12) -> List[float]:
    """Seconds to look at: evenly across the arc, plus a frame at every kill and one just before the first
    (the lead-up is what the sheet is for), de-duplicated and sorted, at most `n` + the kills."""
    a, b = arc["start"], arc["end"]
    ts = [a + (b - a) * i / float(max(1, n - 1)) for i in range(n)]
    ts += [k["t"] + 0.4 for k in arc["kills"]]
    ts.append(max(a, arc["first"] - 1.5))
    out: List[float] = []
    for t in sorted(ts):
        if not out or t - out[-1] >= 0.6:
            out.append(round(min(max(t, a), b), 2))
    return out


def _grab(ffmpeg: str, path: Any, t: float, width: int) -> Any:
    import subprocess

    from PIL import Image

    proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", "{0:.3f}".format(max(0.0, t)), "-i", str(path),
                           "-frames:v", "1", "-vf", "scale={0}:-2".format(width), "-f", "image2pipe", "-vcodec", "png", "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        return None
    import io
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def evidence_sheet(ws: Workspace, settings: Settings, arc: Arc, out_path: Path, resolver: Any = None, n: int = 12,
                   cols: int = 4, tile_w: int = 480) -> Path:
    """Write a contact sheet of an arc: tiles across it with the kills marked, then the transcript lines and
    the score. A YouTube workspace's arc is fetched (once, cached beside the sheet) because no video is on disk."""
    from PIL import Image, ImageDraw, ImageFont

    from . import editoverlay, ytsegments
    from .utils import resolve_tool

    ffmpeg = resolve_tool(settings.tool("ffmpeg"), "install ffmpeg")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    video = ws.video_path()
    offset = 0.0
    if video is None or not Path(video).exists():
        if ws.source_mode() != MODE_EMBED:
            raise StageError("{0} has no video and is not a YouTube workspace".format(ws.slug))
        seg = out_path.parent / "_seg" / "{0}-{1:.0f}-{2:.0f}.mp4".format(arc["id"], arc["start"], arc["end"])
        if not seg.exists():
            seg.parent.mkdir(parents=True, exist_ok=True)
            res = resolver or ytsegments.StreamResolver(ws, settings)
            part = seg.with_name(seg.stem + ".part.mp4")
            ytsegments.fetch_segment(res, arc["start"], arc["end"], part, settings, "libx264", "veryfast", 23)
            part.replace(seg)
        video, offset = seg, arc["start"]

    font_path = editoverlay.find_font()
    font = ImageFont.truetype(font_path, 15) if font_path else ImageFont.load_default()
    small = ImageFont.truetype(font_path, 13) if font_path else ImageFont.load_default()
    kill_times = [k["t"] for k in arc["kills"]]
    tiles = []
    for t in frame_times(arc, n):
        im = _grab(ffmpeg, video, t - offset, tile_w)
        if im is None:
            continue
        d = ImageDraw.Draw(im)
        hit = [k for k in arc["kills"] if abs(k["t"] + 0.4 - t) < 0.05]
        label = "{0}  {1}".format(_mmss(t), "-{0:.0f}s".format(arc["first"] - t) if t < arc["first"] - 0.05 else "+{0:.0f}s".format(t - arc["first"]))
        inside = not arc["kills"] and (arc["first"] > arc["start"] + 0.05 or arc["last"] < arc["end"] - 0.05)
        if inside and arc["first"] - 0.05 <= t <= arc["last"] + 0.05:
            label += "   IN CLIP"
        d.rectangle((0, 0, im.width, 20), fill=(0, 0, 0))
        d.text((4, 3), label, fill=(120, 255, 140) if label.endswith("IN CLIP") else (255, 220, 80), font=small)
        if hit:
            k = hit[0]
            d.rectangle((0, 0, im.width - 1, im.height - 1), outline=(255, 60, 60), width=4)
            tag = "KILL" + (" {0:.0f}m".format(k["dist_m"]) if k.get("dist_m") is not None else "") + (" HEAD" if k.get("headshot") else "") + \
                  (" NADE" if k.get("weapon") == "grenade" else "")
            d.rectangle((0, im.height - 22, im.width, im.height), fill=(180, 0, 0))
            d.text((4, im.height - 19), tag, fill=(255, 255, 255), font=small)
        tiles.append(im)
    if not tiles:
        raise StageError("could not read any frame of {0} between {1:.1f}s and {2:.1f}s".format(ws.slug, arc["start"], arc["end"]))

    tw, th = tiles[0].size
    rows = (len(tiles) + cols - 1) // cols
    ctx = arc.get("context") or context(arc)
    if arc.get("score") is None:
        head = "{0}  {1}  {2}-{3} ({4:.0f}s)".format(arc["id"], ws.slug[:28], _mmss(arc["start"]), _mmss(arc["end"]), ctx["length"])
    else:
        head = "{0}  {1}  {2}-{3} ({4:.0f}s: {5:.0f}s lead-up, {6:.0f}s after)  score {7}: {8}".format(
            arc["id"], ws.slug[:28], _mmss(arc["start"]), _mmss(arc["end"]), ctx["length"], ctx["lead"], ctx["tail"], arc.get("score"),
            " + ".join("{0} {1}".format(k, v) for k, v in (arc.get("parts") or {}).items() if v))
    shown = [l for l in (arc.get("lines") or []) if not _devanagari(l["text"])]
    hidden = len(arc.get("lines") or []) - len(shown)
    text_lines = ["{0}  {1}{2}".format(_mmss(l["t"]), l["text"][:150], "  (?)" if l.get("low") else "") for l in shown]
    if hidden:
        text_lines.append("{0} transcript line(s) in Devanagari are not drawn here: clipbot moments show --workspace {1} --ids {2}".format(
            hidden, ws.slug, arc["id"]))
    for a in arc.get("analysis") or []:
        text_lines.append("analysis: {0}".format(a["description"][:160]))
    foot_h = 22 * (len(text_lines) + 1) + 10
    W = cols * tw
    sheet = Image.new("RGB", (W, 30 + rows * th + foot_h), (14, 14, 18))
    dr = ImageDraw.Draw(sheet)
    dr.text((8, 7), head, fill=(255, 255, 255), font=font)
    for i, im in enumerate(tiles):
        sheet.paste(im, ((i % cols) * tw, 30 + (i // cols) * th))
    y = 30 + rows * th + 6
    for line in text_lines or ["(no transcript lines)"]:
        dr.text((8, y), line, fill=(200, 200, 210), font=small)
        y += 22
    sheet.save(out_path, quality=85)
    return out_path
