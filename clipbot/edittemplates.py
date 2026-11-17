"""Edit templates: build an edit spec from data, so the *taste* lives in one place.

Pure functions that return a plain spec dict for `editspec.normalize`. A template
is where "make it punchier" becomes numbers - the zoom on the kill, the slow-motion
rate, how long a callout stays up - taken from the spec's *profile*
(`editspec.PROFILES`) and from the rules in `.claude/skills/clipbot-edit/rules/`.
Hand-editing the generated spec is expected: a template is a first draft that is
already structurally right, not a black box.

`kill_montage` turns a player's kills (from `killfeed.json`) into a highlight reel:

* kills closer than 12 s become one clip (a multi-kill) so the action is not cut
  in half;
* every clip gets a `kill` marker on the kill, a punch-in and a callout on it, and -
  for grenade kills, headshots and long shots - a speed ramp that slows on the kill
  and snaps back (fast approach, slow payoff, back at speed: the shape in
  rules/motion.md);
* clips are ordered in chapters that build to the grenades, or chronologically, or
  by score;
* an intro card, an outro card, an optional music bed (ducked under the game audio)
  and per-kill sound effects.

episode turns a *lineup* - an author's list of chapters, each a run of clips from any
source - into a numbered variety roundup in the shape of Teo's MISH MASH series: a cold
open, a sting card, chaptered segments (a lower-third names each one) and an end card.
Unlike the montage it does not pick moments (choosing them is the author's job); the
template supplies the structure, the camera life that keeps a talking clip from sitting
still, the chapter markers and the sound.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from . import editbeats, edithl, edithud, editspec, killfeed

# A fifth zoom pulse in one clip is noise, and each camera key costs the renderer a nested
# expression: punch the first few kills of a run and let the callouts carry the rest.
MAX_PUNCHES = 4

ACCENTS = {"nade": "#FF8A00", "headshot": "#FF3B3B", "long": "#35D0FF", "multi": "#B96BFF", "kill": "#FFB300"}

CHAPTER_ORDER = ("HEADSHOTS", "LONG-ISH (50m+)", "LONG SHOTS (100m+)", "MULTI-KILLS & RUNS", "NADES")

# what a viewer reads in the description's chapter list
CHAPTER_TITLES = {"HEADSHOTS": "Headshots", "LONG-ISH (50m+)": "50 m+ kills", "LONG SHOTS (100m+)": "100 m+ shots",
                  "MULTI-KILLS & RUNS": "Multi-kills & runs", "NADES": "Nades"}


def kills_from_killfeed(data: Dict[str, Any], alias: str, root: Optional[Any] = None) -> List[Dict[str, Any]]:
    """The template's kill list from one `killfeed.json`. Icon paths in the file are
    relative to the workspace; `root` (the workspace folder) makes them absolute,
    and an icon that is not on disk is dropped rather than failing the render. A victim
    name that reads as OCR noise is blanked (`killfeed.readable_name`): noise on screen
    is worse than no name."""
    from pathlib import Path

    out = []
    for e in data.get("events", []):
        icon = None
        if e.get("icon") and root is not None:
            p = Path(str(root)) / e["icon"]
            icon = str(p) if p.is_file() else None
        kill = {"id": e.get("id"), "src": alias, "t": float(e["t"]), "dist_m": e.get("dist_m"), "weapon": e.get("weapon", "gun"),
                "headshot": bool(e.get("headshot")), "through_cover": bool(e.get("through_cover")),
                "victim": killfeed.readable_name(e.get("victim") or ""), "icon": icon}
        if e.get("popup"):                  # what the game printed for the kill (the MW4 profile): the `hl` look shows it
            kill["popup"] = e["popup"]
        out.append(kill)
    return out


def kill_score(k: Dict[str, Any]) -> float:
    s = 1.0
    if k.get("weapon") == "grenade":
        s += 5
    if k.get("headshot"):
        s += 2
    if k.get("through_cover"):
        s += 1
    d = k.get("dist_m") or 0
    s += 4 if d >= 100 else 2.5 if d >= 50 else 1 if d >= 25 else 0
    return s


def _segments(kills: List[Dict[str, Any]], profile: Dict[str, Any], merge_gap: float = 12.0) -> List[Dict[str, Any]]:
    """Group a stream's kills into clips: kills within `merge_gap` seconds share one."""
    ordered = sorted(kills, key=lambda k: (k["src"], k["t"]))
    segs: List[Dict[str, Any]] = []
    for k in ordered:
        pre = profile["pre_roll"] + (3.0 if k.get("weapon") == "grenade" else 0.0)
        post = profile["post_roll"] + (1.0 if k.get("weapon") == "grenade" else 0.0)
        last = segs[-1] if segs else None
        if last and last["src"] == k["src"] and k["t"] - last["kills"][-1]["t"] <= merge_gap:
            last["kills"].append(k)
            last["out"] = round(k["t"] + post, 2)
        else:
            segs.append({"src": k["src"], "in": round(max(0.0, k["t"] - pre), 2), "out": round(k["t"] + post, 2), "kills": [k]})
    for n, s in enumerate(segs, start=1):
        s["score"] = sum(kill_score(k) for k in s["kills"]) + 2.0 * (len(s["kills"]) - 1)
        s["id"] = "c{0:02d}".format(n)
    # runs: kills of one stream chained within 50 s of each other
    run_len: Dict[int, int] = {}
    chain: List[int] = []
    for i, s in enumerate(segs):
        if chain and segs[chain[-1]]["src"] == s["src"] and s["kills"][0]["t"] - segs[chain[-1]]["kills"][-1]["t"] <= 50:
            chain.append(i)
        else:
            for j in chain:
                run_len[j] = sum(len(segs[x]["kills"]) for x in chain)
            chain = [i]
    for j in chain:
        run_len[j] = sum(len(segs[x]["kills"]) for x in chain)
    for i, s in enumerate(segs):
        s["run_len"] = run_len.get(i, len(s["kills"]))
        if s["run_len"] >= 3:
            s["score"] += s["run_len"] - 1
    return segs


def _chapter(s: Dict[str, Any]) -> Optional[str]:
    far = max((k.get("dist_m") or 0) for k in s["kills"])
    if any(k.get("weapon") == "grenade" for k in s["kills"]):
        return "NADES"
    if len(s["kills"]) >= 2 or s["run_len"] >= 3:
        return "MULTI-KILLS & RUNS"
    if far >= 100:
        return "LONG SHOTS (100m+)"
    if any(k.get("headshot") for k in s["kills"]):
        return "HEADSHOTS"
    if far >= 50:
        return "LONG-ISH (50m+)"
    return None


def order_segments(segs: List[Dict[str, Any]], order: str, max_clips: Optional[int]) -> List[Dict[str, Any]]:
    """`chapters` (build to the nades, weakest to strongest inside a chapter),
    `chronological`, or `score` (best last). With a limit, keep the best by score and
    re-order those."""
    pool = segs
    if order == "chapters":
        pool = [s for s in segs if _chapter(s)]
    if max_clips and len(pool) > max_clips:
        pool = sorted(pool, key=lambda s: -s["score"])[:max_clips]
    if order == "chronological":
        return sorted(pool, key=lambda s: (s["src"], s["kills"][0]["t"]))
    if order == "score":
        return sorted(pool, key=lambda s: s["score"])
    out: List[Dict[str, Any]] = []
    for name in CHAPTER_ORDER:
        out += sorted([s for s in pool if _chapter(s) == name], key=lambda s: s["score"])
    return out


MULTI_NAMES = {2: "DOUBLE KILL", 3: "TRIPLE KILL", 4: "QUAD KILL", 5: "PENTA KILL"}


def _label(k: Dict[str, Any], ordinal: int) -> Tuple[str, str, str]:
    """(headline, sub line, accent key) for the callout on one kill. `ordinal` is
    this kill's place in its clip (1 = the first): the second kill of a clip is a
    DOUBLE KILL, the third a TRIPLE, so the count builds as the clip plays."""
    d = k.get("dist_m")
    dist = "{0:.0f} m".format(d) if d is not None else ""
    name = (k.get("victim") or "").strip()
    sub = "  ".join(x for x in (dist, name) if x)
    if k.get("weapon") == "grenade":
        return "NADE KILL", sub, "nade"
    if k.get("headshot"):
        return "HEADSHOT", sub, "headshot"
    if ordinal >= 2:
        return MULTI_NAMES.get(ordinal, "{0}-KILL".format(ordinal)), sub, "multi"
    if d is not None and d >= 100:
        return "{0:.0f} M SHOT".format(d), name, "long"
    if d is not None and d >= 50:
        return "{0:.0f} M".format(d), name, "long"
    return "KILL", sub, "kill"


def _slowmo_keys(kill_t: float, lo: float, hi: float, rate: float) -> List[Dict[str, Any]]:
    """Fast, then slow on the kill, then snap back: four keys, all inside [lo, hi]."""
    a, b, c, d = kill_t - 0.7, kill_t - 0.15, kill_t + 0.9, kill_t + 1.5
    if a < lo + 0.05 or d > hi - 0.05:
        return []
    return [{"at": round(a, 3), "rate": 1.0, "ease": "inout2"}, {"at": round(b, 3), "rate": rate, "ease": "out2"},
            {"at": round(c, 3), "rate": rate, "ease": "linear"}, {"at": round(d, 3), "rate": 1.0, "ease": "inout2"}]


def _drift(camera: List[Dict[str, Any]], since: float, kill_t: float, prof: Dict[str, Any]) -> None:
    """A slow push-in across the wait for a kill, ending where the punch begins.

    The approach to a kill - and the stretch between two kills in a run - is the part
    of a clip with the least on screen, so it is where the pacing gate
    (rules/motion.md: "something new every few seconds") fails first. A few percent
    of creeping zoom is motion the viewer feels without noticing, and it hands the
    punch a running start instead of a standing one. Only added when the wait is
    longer than the profile's gap allowance; `since` is where the last move ended.
    """
    lead = kill_t - 0.2 - since
    step = prof["max_gap"] * 0.8
    if lead <= step:
        return
    n = int(math.ceil(lead / step))
    top = 1.0 + (prof["punch"] - 1.0) * 0.35
    for j in range(n + 1):
        at = round(since + lead * j / n, 3)
        if camera and at <= camera[-1]["at"] + 1e-6:
            continue                     # the previous move already ends here
        camera.append({"at": at, "zoom": round(1.0 + (top - 1.0) * j / n, 4), "ease": "inout2"})


def _punch(camera: List[Dict[str, Any]], t: float, zoom: float) -> None:
    """Snap in on a kill at `t` and ease back out. Camera keys must rise in time, so a
    kill that lands inside the previous kill's move rides on it instead."""
    start = round(t - 0.2, 3)
    if camera and camera[-1]["at"] >= t - 1e-6:
        return
    if not camera or camera[-1]["at"] < start - 1e-6:
        camera.append({"at": start, "zoom": 1.0, "ease": "out2"})
    camera += [{"at": round(t, 3), "zoom": zoom, "ease": "out2"},
               {"at": round(t + 0.55, 3), "zoom": zoom, "ease": "linear"},
               {"at": round(t + 1.1, 3), "zoom": 1.0, "ease": "inout2"}]


def kill_clip_parts(cid: str, lo: float, hi: float, kills: List[Dict[str, Any]], prof: Dict[str, Any],
                    slowmo: str = "nade", callout_xy: Tuple[float, float] = (0.5, 0.80),
                    sfx: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """What a clip of one or more kills gets: a marker, a callout, a punch-in and (for
    nades and headshots) a flash on every kill, a slow-motion ramp on the best one, and
    a sound effect. The window is [`lo`, `hi`] in source seconds. Shared by
    `kill_montage` and by an `episode` chapter that carries kills."""
    markers: List[Dict[str, Any]] = []
    overlays: List[Dict[str, Any]] = []
    camera: List[Dict[str, Any]] = []
    fx: List[Dict[str, Any]] = []
    sfx_items: List[Dict[str, Any]] = []
    best = max(kills, key=kill_score)
    punches = 0
    for i, k in enumerate(kills):
        t = k["t"]
        head, sub, accent = _label(k, i + 1)
        markers.append({"at": round(t, 3), "name": "kill"})
        ov: Dict[str, Any] = {"kind": "callout", "at": round(t, 3), "dur": 2.2, "text": head, "accent": ACCENTS[accent],
                              "x": callout_xy[0], "y": callout_xy[1], "size": 0.050, "anim_in": "pop", "anim_out": "fade"}
        if sub:
            ov["sub"] = sub
        if k.get("icon"):
            ov["image"] = k["icon"]
        if i > 0 and t - kills[i - 1]["t"] < 2.4:  # two callouts must not overlap on screen
            ov["y"] = round(callout_xy[1] - 0.12, 3)
        overlays.append(ov)
        if prof["punch"] > 1.0 and punches < MAX_PUNCHES:
            _drift(camera, camera[-1]["at"] if camera else lo, t, prof)
            _punch(camera, t, prof["punch"])
            punches += 1
        if prof["flash"] and (k.get("weapon") == "grenade" or k.get("headshot")):
            fx.append({"type": "flash", "at": round(t, 3), "dur": 0.12, "amount": 0.45})
        kind = "hit"
        if k.get("weapon") == "grenade" and sfx and "nade" in sfx:
            kind = "nade"
        elif k.get("headshot") and sfx and "headshot" in sfx:
            kind = "headshot"
        if sfx and kind in sfx:
            sfx_items.append(dict(sfx[kind], id="s{0}_{1}".format(cid, i), on={"clip": cid, "at": round(t, 3)}))
    want_slow = (slowmo == "all") or (slowmo == "nade" and best.get("weapon") == "grenade") or                 (slowmo == "special" and (best.get("weapon") == "grenade" or best.get("headshot") or (best.get("dist_m") or 0) >= 100))
    speed = _slowmo_keys(best["t"], lo, hi, prof["slowmo_rate"]) if want_slow else []
    return {"markers": markers, "overlays": overlays, "camera": camera, "fx": fx, "speed": speed, "sfx": sfx_items}


def kill_montage(
    kills: List[Dict[str, Any]],
    sources: Dict[str, Dict[str, Any]],
    name: str = "kill-montage",
    player: str = "Player",
    game: str = "",
    profile: str = "punchy",
    order: str = "chapters",
    max_clips: Optional[int] = None,
    slowmo: str = "nade",
    intro: bool = True,
    outro: bool = True,
    music: Optional[Dict[str, Any]] = None,
    sfx: Optional[Dict[str, Dict[str, Any]]] = None,
    grade: str = "gamer_pop",
    callout_xy: Tuple[float, float] = (0.5, 0.80),
    output_preset: str = "youtube_1080p60",
    facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build an edit spec from kills.

    `kills` items: {src: alias, t, dist_m, weapon, headshot, through_cover, victim,
    icon (absolute path or None)}. `sources` is the spec's `sources` block. `slowmo`
    is `none`, `nade` (grenade kills only), `special` (nade, headshot, 100 m+) or
    `all`. `music` is {"file"|"asset": ..., "gain_db": ...}; `sfx` maps `hit` /
    `nade` / `headshot` to {"file"|"asset": ...}. If `facts` is a dict it is filled
    with what the edit actually contains - `kills`, `nades`, `far` (metres) - which
    is not what was passed in: a chaptered cut leaves the plain kills out, and a
    title written from the input count would claim kills the viewer never sees.
    """
    prof = editspec.PROFILES[profile]
    segs = _segments(kills, prof)
    chosen = order_segments(segs, order, max_clips)
    if not chosen:
        raise editspec.SpecError("no clips to build: the kill list is empty (or none qualify for the chapters)")

    clips: List[Dict[str, Any]] = []
    sfx_items: List[Dict[str, Any]] = []
    transition = None
    if prof["transition"] > 0:
        transition = {"type": "xfade", "name": "fade", "dur": prof["transition"]}

    last_chapter = None
    for n, s in enumerate(chosen, start=1):
        cid = "k{0:02d}".format(n)
        lo, hi = s["in"], s["out"]
        parts = kill_clip_parts(cid, lo, hi, s["kills"], prof, slowmo, callout_xy, sfx)
        markers, overlays, camera, fx, speed = parts["markers"], parts["overlays"], parts["camera"], parts["fx"], parts["speed"]
        sfx_items += parts["sfx"]
        chap = _chapter(s) if order == "chapters" else None
        starts_chapter = bool(chap) and chap != last_chapter
        if chap:
            last_chapter = chap
        tags = (["nade"] if any(k.get("weapon") == "grenade" for k in s["kills"]) else []) + \
               (["headshot"] if any(k.get("headshot") for k in s["kills"]) else []) + \
               (["chapter"] if starts_chapter else [])
        clip = {"id": cid, "src": s["src"], "in": lo, "out": hi,
                "label": CHAPTER_TITLES[chap] if starts_chapter else "{0} {1}".format(_chapter(s) or "kill", cid),
                "tags": tags,
                "markers": markers, "overlays": overlays, "camera": camera, "fx": fx,
                "grade": {"preset": grade, "strength": "normal"}, "audio": {"slowmo": "pitch"}}
        if speed:
            clip["speed"] = speed
            clip["interp"] = "blend"
        if transition and n > 1:
            clip["transition_in"] = dict(transition)
        clips.append(clip)

    cards_first: List[Dict[str, Any]] = []
    cards_last: List[Dict[str, Any]] = []
    n_kills = sum(len(s["kills"]) for s in chosen)
    n_nades = sum(1 for s in chosen for k in s["kills"] if k.get("weapon") == "grenade")
    if facts is not None:
        dists = [k["dist_m"] for s in chosen for k in s["kills"] if k.get("dist_m") is not None]
        facts.update({"kills": n_kills, "nades": n_nades, "far": max(dists) if dists else None})
    if intro:
        sub = "{0} kill{1}".format(n_kills, "" if n_kills == 1 else "s") + \
              ("  /  {0} nade{1}".format(n_nades, "" if n_nades == 1 else "s") if n_nades else "")
        cards_first.append({"id": "intro", "type": "card", "dur": 2.2, "bg": "#0B0B0F", "label": "Intro", "tags": ["chapter"],
                            "overlays": [
                                {"kind": "text", "at": 0.0, "dur": 2.2, "text": (player + " - " + game).strip(" -").upper(),
                                 "x": 0.5, "y": 0.45, "size": 0.10, "anim_in": "pop", "anim_out": "fade"},
                                {"kind": "text", "at": 0.25, "dur": 1.95, "text": sub.upper(), "x": 0.5, "y": 0.58, "size": 0.045,
                                 "color": "#FFB300", "anim_in": "slide_up", "anim_out": "fade"}]})
    if outro:
        cards_last.append({"id": "outro", "type": "card", "dur": 2.4, "bg": "#0B0B0F", "label": "Outro", "tags": ["nochapter"],
                           "overlays": [{"kind": "text", "at": 0.0, "dur": 2.4, "text": player.upper(), "x": 0.5, "y": 0.5,
                                         "size": 0.09, "anim_in": "pop", "anim_out": "fade"}]})
    all_clips = cards_first + clips + cards_last

    spec: Dict[str, Any] = {
        "version": editspec.VERSION, "name": name, "profile": profile,
        "output": {"preset": output_preset},
        "sources": sources, "clips": all_clips,
        "tracks": {"music": [], "sfx": sfx_items},
        "master": {"loudness_lufs": -14.0, "true_peak_db": -1.0},
        "notes": "Generated by edittemplates.kill_montage ({0} kills, {1} grenade, order={2}, slowmo={3}).".format(
            n_kills, n_nades, order, slowmo),
    }
    if music:
        spec["tracks"]["music"].append(dict(music, id="bed", duck=music.get("duck", True)))
    return spec


# ---------------------------------------------------------------------------
# episode: a variety roundup built from an author's lineup
# ---------------------------------------------------------------------------

EPISODE_ACCENT = "#FFB300"
LABEL_Y = 0.10          # top centre: clear of a webcam on either side and of the chat at the bottom


MIN_LEAD = 10.0          # seconds of lead-up a kill clip must give the first kill (rules/roundups.md: context)
MIN_TAIL = 5.0           # ...and of aftermath after the last


def _lineup_clip_lists(lineup: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    out: List[Tuple[str, List[Dict[str, Any]]]] = []
    cold = lineup.get("cold_open")
    if cold and cold.get("clips"):
        out.append(("cold open", cold["clips"]))
    for ch in lineup.get("chapters", []):
        out.append((ch.get("title", "chapter"), ch.get("clips", [])))
    return out


def context_issues(lineup: Dict[str, Any], min_lead: float = MIN_LEAD, min_tail: float = MIN_TAIL) -> List[str]:
    """Kill clips that show the kill without what leads to it or what follows.

    A clip of a kill with six seconds before it and three after was the shape of the first episode's
    complaint: the kill without the fight. Each clip with `kills` must start `min_lead` seconds before
    the first and end `min_tail` after the last, unless the author marks it `"cutaway": true` (a deliberate
    insert). Returns one sentence per clip that falls short; an empty list is a pass.
    """
    out: List[str] = []
    for title, clips in _lineup_clip_lists(lineup):
        for i, c in enumerate(clips, start=1):
            if not c.get("kills") or c.get("cutaway"):
                continue
            first = min(float(k["t"]) for k in c["kills"])
            last = max(float(k["t"]) for k in c["kills"])
            lead, tail = first - float(c["in"]), float(c["out"]) - last
            short = []
            if lead < min_lead - 1e-6:
                short.append("{0:.1f} s before the first kill (want {1:g})".format(lead, min_lead))
            if tail < min_tail - 1e-6:
                short.append("{0:.1f} s after the last (want {1:g})".format(tail, min_tail))
            if short:
                out.append("{0!r} clip {1} ({2} {3:.0f}-{4:.0f}): {5}".format(title, i, c.get("src"), c["in"], c["out"], "; ".join(short)))
    return out


def expand_moments(lineup: Dict[str, Any], moments_for: Any, kills_for: Any) -> List[str]:
    """Fill in clips that name a moment: `{"src": alias, "moment": "m004", "trim": [lead_cut, tail_cut]}`.

    `moments_for(alias)` returns that source's `moments.json` document (or None) and `kills_for(alias)` a
    map of kill id -> kill dict (with the weapon icon's path where the feed had one). The clip's `in`/`out`
    become the arc's window less `trim` (seconds taken off the lead-up and the aftermath), its `kills` the
    arc's kills inside that window, and `moment` stays on the clip as a record. Changes the lineup in place;
    returns a line per clip it filled. An unknown source or id is a `SpecError` naming it.
    """
    done: List[str] = []
    for title, clips in _lineup_clip_lists(lineup):
        for i, c in enumerate(clips, start=1):
            mid = c.get("moment")
            if not mid and c.get("kill_ids"):
                # a window chosen by hand around kills named by id (`killfeed.json` ids): the kills come from the feed,
                # with their weapon icons, so a clip that is not a whole arc still gets its contact plates
                known = kills_for(c.get("src")) or {}
                missing = [k for k in c["kill_ids"] if k not in known]
                if missing:
                    raise editspec.SpecError("{0!r} clip {1}: no kill {2} in {3}'s killfeed.json".format(title, i, ", ".join(missing), c.get("src")))
                c["kills"] = [dict(known[k], src=c["src"]) for k in c["kill_ids"]]
                done.append("{0!r} clip {1}: {2} {3} -> {4} kill(s) by id".format(title, i, c["src"], "-".join(c["kill_ids"]), len(c["kills"])))
                continue
            if not mid:
                continue
            alias = c.get("src")
            doc = moments_for(alias)
            where = "{0!r} clip {1}".format(title, i)
            if doc is None:
                raise editspec.SpecError("{0}: source {1!r} has no moments.json (clipbot moments build --workspace ...)".format(where, alias))
            arc = next((a for a in doc["arcs"] if a["id"] == mid), None)
            if arc is None:
                raise editspec.SpecError("{0}: no moment {1!r} in {2} ({3} arcs)".format(where, mid, alias, len(doc["arcs"])))
            if c.get("at") is not None and abs(float(arc["first"]) - float(c["at"])) > 2.0:
                raise editspec.SpecError("{0}: moment {1} of {2} now starts at its first kill {3:.1f} s, not {4:.1f} s as the lineup recorded - "
                                         "the ids shifted after a rescan; look the moment up again".format(where, mid, alias, float(arc["first"]), float(c["at"])))
            trim = c.get("trim") or [0.0, 0.0]
            lo, hi = float(arc["start"]) + float(trim[0]), float(arc["end"]) - float(trim[1])
            if hi - lo < 2.0:
                raise editspec.SpecError("{0}: trim {1} leaves under 2 s of moment {2}".format(where, trim, mid))
            known = kills_for(alias) or {}
            c["in"], c["out"] = round(lo, 2), round(hi, 2)
            c["kills"] = [dict(known.get(k["id"]) or {}, **{kk: k[kk] for kk in ("t", "dist_m", "weapon", "headshot", "through_cover")},
                               src=alias) for k in arc["kills"] if lo <= k["t"] <= hi]
            c.setdefault("snap", "words")          # the arc's edges were already moved into pauses; only words may still move them
            done.append("{0}: {1} {2} -> {3:.1f}-{4:.1f} s, {5} kill(s)".format(where, alias, mid, lo, hi, len(c["kills"])))
    return done


def _ambient_camera(lo: float, hi: float, prof: Dict[str, Any], punches: List[Any]) -> List[Dict[str, Any]]:
    """Camera life for a clip with no kill in it: a slow push-in across the whole clip
    (so a talking head over a map never sits still for longer than the profile's gap)
    and a snap-in on each beat in `punches` (source seconds - the punchline, the reveal).

    The push is a few percent of zoom, felt rather than seen; a key lands at least every
    0.8 x the profile's gap because the pacing gate counts keys, not intentions.
    """
    dur = max(hi - lo, 0.1)
    step = prof["max_gap"] * 0.8
    n = max(1, int(math.ceil(dur / step)))
    top = 1.0 + (prof["punch"] - 1.0) * 0.6

    def base(t: float) -> float:
        return 1.0 + (top - 1.0) * min(max((t - lo) / dur, 0.0), 1.0)

    keys: List[Tuple[float, float, str]] = [(lo + dur * j / n, base(lo + dur * j / n), "inout2") for j in range(n + 1)]
    pop_default = prof["punch"] - 1.0
    for item in sorted(punches, key=lambda x: x[0] if isinstance(x, tuple) else x):
        p, pop = item if isinstance(item, tuple) else (item, pop_default)       # (second, extra zoom) or just a second
        if not (lo + 0.3 < p < hi - 0.3):
            continue
        end = min(p + 1.0, hi)
        keys += [(p - 0.2, base(p - 0.2), "out2"), (p, base(p) + pop, "out2"),
                 (p + 0.5, base(p + 0.5) + pop, "linear"), (end, base(end), "inout2")]
    keys.sort(key=lambda k: k[0])
    out: List[Dict[str, Any]] = []
    for at, zoom, ease in keys:
        if out and at < out[-1]["at"] + 0.08:
            if zoom > out[-1]["zoom"]:           # a punch wins over the push it lands on
                out[-1] = {"at": out[-1]["at"], "zoom": round(zoom, 4), "ease": ease}
            continue
        out.append({"at": round(at, 3), "zoom": round(zoom, 4), "ease": ease})
    return out[:editspec.MAX_CAMERA_KEYS]


def _title_overlay(text: str, at: float, y: float = LABEL_Y, dur: float = 2.6,
                   brand: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ov = {"kind": "text", "at": round(at, 3), "dur": dur, "text": text.upper(), "x": 0.5, "y": round(y, 3), "size": 0.050,
          "plate": True, "anim_in": "slide_down", "anim_out": "fade"}
    if brand:
        if brand.get("label_style"):
            ov["style"] = brand["label_style"]
            ov["size"] = 0.056              # a condensed display face reads smaller at the same size
        if brand.get("accent"):
            ov["accent"] = brand["accent"]
    return ov


def _card(cid: str, dur: float, label: str, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": cid, "type": "card", "dur": dur, "bg": "#0B0B0F", "label": label, "tags": ["nochapter"], "overlays": lines}


def _card_lines(series: str, number: Optional[int], dur: float, big: float, small: float, y: float,
                brand: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The series name, and under it the episode number in the accent colour. Nothing stays
    up longer than the pacing gate's dwell limit: a long end card is dark with the music
    under it, which is also where YouTube's end-screen elements go. A brand kit supplies the
    title typeface and the accent."""
    brand = brand or {}
    hold = min(dur, 3.8)
    accent = brand.get("accent") or EPISODE_ACCENT
    title = {"kind": "text", "at": 0.0, "dur": hold, "text": series.upper(), "x": 0.5, "y": y if number else 0.5,
             "size": big, "anim_in": "pop", "anim_out": "fade"}
    if brand.get("title_style"):
        title["style"] = brand["title_style"]
    lines = [title]
    if number:
        num = {"kind": "text", "at": 0.3, "dur": hold - 0.3, "text": "#{0}".format(number), "x": 0.5,
               "y": round(y + 0.15, 3), "size": small, "color": accent, "anim_in": "slide_up", "anim_out": "fade"}
        if brand.get("title_style"):
            num["style"] = brand["title_style"]
        lines.append(num)
    return lines


def episode(lineup: Dict[str, Any], facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build an edit spec from a lineup.

    lineup = {
      name, series ("TECK MISH MASH"), number (1), title (None until the author has one),
      profile ("restrained"), output_preset, grade, sources {alias: {...}},
      cold_open: {title, clips: [...]} | None,
      sting: {dur, music: {asset|file, gain_db}} | None,
      chapters: [{title, label (bool, default True), whoosh (bool, default True),
                  music: {asset|file, gain_db} | None, clips: [...]}],
      end: {dur, music: {...}} | None,
      sfx: {"sting": {...}, "chapter": {...}}, kill_sfx: {"hit": {...}, ...}, slowmo,
      brand: {accent, label_style, title_style, styles {id: text style}}, glue (default True),
      beat_sfx: {"hit"|"whoosh"|"glitch"|"scratch": {asset|file, gain_db}}, beat_stickers: {role: asset id} }
    A chapter may carry `transition`: whip | glitch | zoom_through | flash | dip | crossfade (editbeats).
    A clip's `slowmo` ("none"|"nade"|"special"|"all") overrides the lineup's for that clip (a montage of kills plays at speed).
    A clip is {src, in, out, note, label (a lower-third line), punch [source seconds], beats [{do, at, ...}] (named moves,
    editbeats: freeze_zoom, impact, caption_pop, replay),
    kills [killfeed events - the clip then gets callouts, punches and a slow-mo ramp like
    a montage], gain_db, speed, ambient (default True)}. Positions are source seconds,
    as everywhere in the engine. `facts` (if a dict) is filled with what the edit
    contains, for `edit deliver`.
    """
    prof = editspec.PROFILES[lineup.get("profile", "restrained")]
    series = lineup.get("series", "MISH MASH")
    number = lineup.get("number")
    grade = lineup.get("grade", "neutral_punch")
    brand = lineup.get("brand") or {}
    sfx_lib = lineup.get("sfx") or {}
    clips: List[Dict[str, Any]] = []
    sfx_items: List[Dict[str, Any]] = []
    music_items: List[Dict[str, Any]] = []
    chapter_list: List[Dict[str, Any]] = []
    counter = {"n": 0, "contact": 0}

    warnings: List[str] = []

    # the HUD look (edithud): motion graphics in place of the stills; the lineup's `hud_sfx` names the sounds
    hud = lineup.get("look") == "hud"
    hud_sfx = lineup.get("hud_sfx") or {}
    # the `hl` look (edithl): the channel's own graphics - kill plates in the game's words, chapter headers, tags; `hl_sfx`
    # names the sounds that land with them
    hl = lineup.get("look") == "hl"
    hl_sfx = lineup.get("hl_sfx") or {}
    hl_meta = lineup.get("hl") or {}
    all_kills = [k for _, cl in _lineup_clip_lists(lineup) for c in cl for k in (c.get("kills") or [])]
    kills_by_op: Dict[int, List[Dict[str, Any]]] = {}

    def hud_contacts(cid: str, c: Dict[str, Any], ramp: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """A contact plate (and the sound that lands with it) for every kill of a clip. Two kills closer than a plate's
        life stack one under the other; the clip's `hero` kill (default: its longest) gets the large plate."""
        ov: List[Dict[str, Any]] = []
        snd: List[Dict[str, Any]] = []
        kills = c["kills"]
        hero = None
        if c.get("hero"):
            hero = max(range(len(kills)), key=lambda j: kills[j].get("dist_m") or 0)
        prev_t, row, chain = None, 0, 0
        for i, k in enumerate(kills):
            counter["contact"] += 1
            gap = None if prev_t is None else k["t"] - prev_t
            # DOUBLE KILL / TRIPLE KILL only for kills a few seconds apart: seven seconds apart is a run, not a double
            chain = chain + 1 if gap is not None and gap <= edithud.MULTI_WINDOW else 1
            head, _sub, accent = _label(k, chain)
            # a slow-motion ramp stretches the output gap beyond the source gap, so only a very close pair can overlap there
            row = row + 1 if gap is not None and gap < (1.0 if ramp else edithud.CONTACT_DUR - 0.2) else 0
            y = 72 + 372 * row if row else None
            ov.append(edithud.contact(k, head, accent, counter["contact"], len(all_kills), y_px=y, hero=(i == hero),
                                      note=c.get("hero_note", "LONGEST CONFIRMED") if i == hero else None))
            if hud_sfx.get("contact"):
                snd.append(dict(hud_sfx["contact"], id="ct{0}_{1}".format(cid, i), on={"clip": cid, "at": round(k["t"], 3)}, align="peak"))
            prev_t = k["t"]
        return ov, snd

    def hl_plates(cid: str, c: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """A kill plate (and the sound that lands with it) for every kill of a clip. A plate is cut short when the next
        kill comes before it would be gone, so two never share the screen."""
        ov: List[Dict[str, Any]] = []
        snd: List[Dict[str, Any]] = []
        kills = c["kills"]
        durs = edithl.plate_durations([k["t"] for k in kills])
        prev_t, chain = None, 0
        for i, k in enumerate(kills):
            counter["contact"] += 1
            gap = None if prev_t is None else k["t"] - prev_t
            chain = chain + 1 if gap is not None and gap <= edithud.MULTI_WINDOW else 1
            ov.append(edithl.kill_plate(k, counter["contact"], len(all_kills), chain=chain, dur=durs[i]))
            if hl_sfx.get("kill"):
                snd.append(dict(hl_sfx["kill"], id="pl{0}_{1}".format(cid, i), on={"clip": cid, "at": round(k["t"], 3)}, align="peak"))
            prev_t = k["t"]
        return ov, snd

    def clip_from(c: Dict[str, Any], prefix: str, title: Optional[str], chapter_label: bool, whoosh: bool,
                  op: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        counter["n"] += 1
        cid = "{0}{1:02d}".format(prefix, counter["n"])
        lo, hi = float(c["in"]), float(c["out"])
        overlays: List[Dict[str, Any]] = []
        markers: List[Dict[str, Any]] = []
        fx: List[Dict[str, Any]] = []
        speed = c.get("speed") or []
        camera: List[Dict[str, Any]] = []
        y = LABEL_Y
        # named moves (editbeats): a freeze-zoom on the punchline, an impact, a caption on the beat, a replay after it.
        # Expanded first: their zooms are camera keys, merged with the clip's own camera below
        grade_d = {"preset": grade, "strength": "subtle"}
        bparts: Dict[str, Any] = {}
        for n, beat in enumerate(c.get("beats") or []):
            ctx = editbeats.Ctx(cid, c["src"], lo, hi, sfx=lineup.get("beat_sfx"), stickers=lineup.get("beat_stickers"), brand=brand,
                                grade=grade_d, gain_db=float(c.get("gain_db", 0.0)), n=n, max_gap=prof["max_gap"])
            editbeats.merge(bparts, editbeats.expand(beat, ctx))
        if title and chapter_label and hi - lo >= 2.5 and hud and op:
            overlays.append(edithud.op_title(op["no"], title, op["sub"], lo + edithud.OP_LANDING))
            if hud_sfx.get("op"):
                sfx_items.append(dict(hud_sfx["op"], id="op" + cid, on={"clip": cid, "at": round(lo + edithud.OP_LANDING, 3)}, align="peak"))
        elif title and chapter_label and hi - lo >= 2.5 and hl and op:
            overlays.append(edithl.chapter_header(op["no"], title, op["sub"], lo + edithl.CHAPTER_LANDING))
            if hl_sfx.get("chapter"):
                sfx_items.append(dict(hl_sfx["chapter"], id="ch" + cid, on={"clip": cid, "at": round(lo + edithl.CHAPTER_LANDING, 3)}, align="peak"))
        elif title and chapter_label and hi - lo >= 2.5:
            overlays.append(_title_overlay(title, lo + 0.4, y, brand=brand))
            y += 0.075
        if c.get("label") and hi - lo >= 2.5 and hl:
            shares = bool(title and chapter_label and hi - lo >= 2.5 and op)          # the chapter header is up in the same corner
            overlays.append(edithl.tag(c["label"], lo + (1.0 if shares else 0.5), tone=c.get("tone", "cyan"),
                                       y=edithl.BELOW_CHAPTER_Y if shares else None))
        elif c.get("label") and hi - lo >= 2.5:
            overlays.append(_title_overlay(c["label"], lo + (0.4 if y == LABEL_Y else 0.7), y, brand=brand))
        for m in c.get("motion") or []:                 # a graphic the author places by hand: {comp, at, [hit, dur], props}
            overlays.append(dict(m, kind="motion"))
        if c.get("kills"):
            parts = kill_clip_parts(cid, lo, hi, c["kills"], prof, c.get("slowmo", lineup.get("slowmo", "nade")), (0.5, 0.80),
                                    lineup.get("kill_sfx"))
            if hud:
                plates, plate_sfx = hud_contacts(cid, c, ramp=bool(c.get("speed") or parts["speed"]))
                overlays += plates
                sfx_items.extend(plate_sfx)
                if parts["camera"]:
                    _drift(parts["camera"], parts["camera"][-1]["at"], hi + 0.2, prof)      # the aftermath keeps moving, so it is not a dead stretch
            elif hl:
                plates, plate_sfx = hl_plates(cid, c)
                overlays += plates
                sfx_items.extend(plate_sfx)
                if parts["camera"]:
                    _drift(parts["camera"], parts["camera"][-1]["at"], hi + 0.2, prof)
            else:
                overlays += parts["overlays"]
            markers += parts["markers"]
            fx += parts["fx"]
            camera = parts["camera"]
            for pun in bparts.get("punches", []):
                _punch(camera, pun["at"], pun["zoom"])
            speed = speed or parts["speed"]
            sfx_items.extend(parts["sfx"])
        elif c.get("ambient", True):
            camera = _ambient_camera(lo, hi, prof, [float(p) for p in c.get("punch", [])]
                                     + [(p["at"], p["zoom"] - 1.0) for p in bparts.get("punches", [])])
            markers += [{"at": round(float(p), 3), "name": "punch"} for p in c.get("punch", []) if lo < float(p) < hi]
        clip: Dict[str, Any] = {
            "id": cid, "src": c["src"], "in": lo, "out": hi,
            "label": (title if title else str(c.get("note") or cid))[:80],
            "tags": ["chapter"] if title else [],
            "overlays": overlays, "markers": markers, "camera": camera, "fx": fx,
            "grade": grade_d,
            "audio": {"slowmo": "pitch", "gain_db": float(c.get("gain_db", 0.0))},
        }
        if speed:
            clip["speed"] = speed
            clip["interp"] = "blend"
        if title and whoosh and sfx_lib.get("chapter"):
            sfx_items.append(dict(sfx_lib["chapter"], id="w" + cid, on={"clip": cid, "at": round(lo + 0.4, 3)}))
        extra: List[Dict[str, Any]] = []
        if bparts:
            if bparts.get("freezes"):
                clip["freezes"] = bparts["freezes"]
            clip["fx"] = clip["fx"] + bparts.get("fx", [])
            clip["overlays"] = clip["overlays"] + bparts.get("overlays", [])
            clip["markers"] = clip["markers"] + bparts.get("markers", [])
            sfx_items.extend(bparts.get("sfx", []))
            extra = bparts.get("after", [])
            warnings.extend("{0}: {1}".format(cid, w) for w in bparts.get("warnings", []))
        return clip, extra

    def run(chapter: Dict[str, Any], prefix: str, label: bool, op_no: Optional[int] = None) -> None:
        first = len(clips)
        ctitle = chapter.get("title", "Cold open")
        # a chapter that asks for a transition preset gets that sound, not the chapter whoosh as well, unless it says `whoosh`
        whoosh = chapter.get("whoosh", not chapter.get("transition"))
        chapter_kills = [k for c in chapter["clips"] for k in (c.get("kills") or [])]
        op = None
        if hud and op_no is not None:
            kills_by_op[op_no] = chapter_kills
            op = {"no": op_no, "sub": chapter["sub"] if "sub" in chapter else edithud.op_sub(chapter_kills)}
        elif hl and op_no is not None:
            op = {"no": op_no, "sub": chapter["sub"] if "sub" in chapter else edithl.chapter_sub(chapter_kills)}
        for i, c in enumerate(chapter["clips"]):
            clip, extra = clip_from(c, prefix, ctitle if i == 0 else None, label, whoosh, op)
            clips.append(clip)
            clips.extend(extra)
        if chapter.get("transition") and len(clips) > first:
            res = editbeats.apply_transition(chapter["transition"], clips[first - 1] if first else None, clips[first], lineup.get("beat_sfx"))
            sfx_items.extend(res["sfx"])
            warnings.extend("{0}: {1}".format(ctitle, w) for w in res["warnings"])
        mus = chapter.get("music")
        if mus and len(clips) > first:
            music_items.append(dict(mus, id="bed{0}".format(len(music_items) + 1),
                                    start={"clip": clips[first]["id"]}, end={"clip": clips[-1]["id"], "from": "end"},
                                    duck=mus.get("duck", True)))
        chapter_list.append({"title": ctitle, "clips": len(chapter["clips"])})

    cold = lineup.get("cold_open")
    if cold and cold.get("clips"):
        run(cold, "o", False)
    sting = lineup.get("sting")
    hud_meta = lineup.get("hud") or {}
    if sting is not None:
        dur = float(sting.get("dur", edithud.TITLE_DUR if hud else edithl.TITLE_DUR if hl else 3.0))
        if hl:
            clips.append(edithl.title_card(series, hl_meta.get("kicker", ""), hl_meta.get("sub", ""), hl_meta.get("tag", ""), dur))
            if hl_sfx.get("title") or sfx_lib.get("sting"):
                sfx_items.append(dict(hl_sfx.get("title") or sfx_lib["sting"], id="sting", on={"clip": "sting", "at": edithl.TITLE_HIT}, align="peak"))
        elif hud:
            n_streams = len({c["src"] for _, cl in _lineup_clip_lists(lineup) for c in cl if c.get("src")})
            n_ops = sum(1 for ch in lineup.get("chapters", []) if ch.get("label", True))
            meta = hud_meta.get("meta") or ["REPORT {0:02d}".format(int(number)) if number is not None else "REPORT",
                                            "{0:02d} STREAMS".format(n_streams), "{0:02d} OPERATIONS".format(n_ops),
                                            "{0:02d} CONTACTS".format(len(all_kills))]
            clips.append(edithud.title_card(series, number, hud_meta.get("kicker", "FIELD REPORT"), hud_meta.get("sub", ""), dur, meta=meta))
            if hud_sfx.get("title"):
                sfx_items.append(dict(hud_sfx["title"], id="sting", on={"clip": "sting", "at": edithud.TITLE_HIT}, align="peak"))
        else:
            clips.append(_card("sting", dur, "Intro", _card_lines(series, number, dur, 0.12, 0.08, 0.44, brand)))
            if sfx_lib.get("sting"):
                sfx_items.append(dict(sfx_lib["sting"], id="sting", on={"clip": "sting"}))
        if sting.get("music"):                  # a few seconds of theme, so the card is not a silent gap
            music_items.append(dict(sting["music"], id="stingbed", start={"clip": "sting"},
                                    end={"clip": "sting", "from": "end"}, fade_in=0.0, fade_out=0.4, duck=False))
    chapter_ids: Dict[int, Tuple[str, str]] = {}
    for n, chapter in enumerate(lineup.get("chapters", []), start=1):
        first_clip = len(clips)
        run(chapter, "c{0}_".format(n), chapter.get("label", True), op_no=n if chapter.get("label", True) else None)
        if len(clips) > first_clip:
            chapter_ids[n] = (clips[first_clip]["id"], clips[-1]["id"])
    for b, bed in enumerate(lineup.get("beds") or [], start=1):
        # one music bed under several chapters (a chapter's own `music` restarts the track at every chapter)
        a, z = int(bed["from_chapter"]), int(bed.get("to_chapter", bed["from_chapter"]))
        if a not in chapter_ids or z not in chapter_ids:
            raise editspec.SpecError("beds[{0}]: chapters {1}..{2} are not all in the lineup".format(b - 1, a, z))
        item = {k: v for k, v in bed.items() if k not in ("from_chapter", "to_chapter")}
        music_items.append(dict(item, id="mbed{0}".format(b), start={"clip": chapter_ids[a][0]},
                                end={"clip": chapter_ids[z][1], "from": "end"}, duck=bed.get("duck", True)))
    streams =len({c["src"] for _, cl in _lineup_clip_lists(lineup) for c in cl if c.get("src")})
    tally = edithud.tally(all_kills, streams) if hud else None
    end = lineup.get("end")
    if end is not None:
        dur = float(end.get("dur", edithud.STATS_DUR if hud else edithl.END_DUR if hl else 6.0))
        if hl:
            stats = hl_meta.get("stats") or edithl.default_stats(edithl.tally(all_kills))
            clips.append(edithl.end_card(stats, hl_meta.get("end_line", ""), hl_meta.get("foot", ""),
                                         hl_meta.get("end_title", "THE NIGHT IN NUMBERS"), dur))
            if hl_sfx.get("end"):
                sfx_items.append(dict(hl_sfx["end"], id="endstat", on={"clip": "end", "at": edithl.END_HIT}, align="peak"))
        elif hud:
            ops = [{"n": n, "name": ch["title"].upper(), "count": len(kills_by_op.get(n) or []) or len(ch["clips"])}
                   for n, ch in enumerate(lineup.get("chapters", []), start=1) if ch.get("label", True)]
            clips.append(edithud.stats_card(series, number, tally, ops, dur, title=hud_meta.get("end_title", "MISSION COMPLETE"),
                                            next_label=hud_meta.get("next", "NEXT REPORT")))
            if hud_sfx.get("ratchet"):
                sfx_items.append(dict(hud_sfx["ratchet"], id="endratchet", on={"clip": "end", "at": edithud.STATS_HIT + 0.15}, align="start"))
            if hud_sfx.get("stamp"):
                sfx_items.append(dict(hud_sfx["stamp"], id="endstamp", on={"clip": "end", "at": edithud.STATS_HIT}, align="peak"))
        else:
            clips.append(_card("end", dur, "End card", _card_lines(series, number, dur, 0.09, 0.06, 0.46, brand)))
        if end.get("music"):
            music_items.append(dict(end["music"], id="endbed", start={"clip": "end"}, end="end", fade_out=1.5, duck=False))
    n_clips = sum(1 for c in clips if c.get("type", "clip") == "clip")
    if not n_clips:
        raise editspec.SpecError("the lineup has no clips")

    title = lineup.get("title")
    if facts is not None:
        facts.update({"series": series, "number": number, "title": title, "chapters": chapter_list, "clips": n_clips,
                      "warnings": warnings})
        if tally:
            facts["tally"] = tally
    spec = {
        "version": editspec.VERSION, "name": lineup["name"], "profile": lineup.get("profile", "restrained"),
        "output": {"preset": lineup.get("output_preset", "youtube_1080p60")},
        "sources": lineup["sources"], "clips": clips,
        "tracks": {"music": music_items, "sfx": sfx_items},
        "master": {"loudness_lufs": -14.0, "true_peak_db": -1.0, "glue": bool(lineup.get("glue", True))},
        "notes": "Generated by edittemplates.episode ({0} #{1}, {2} chapters, {3} clips). Title: {4}.".format(
            series, number, len(chapter_list), n_clips, title or "not chosen yet"),
    }
    if lineup.get("captions") is not None:
        caps = dict(lineup["captions"])
        if brand.get("accent"):
            caps.setdefault("highlight", brand["accent"])        # the spoken word lights up in the brand's colour
        spec["captions"] = caps
    return spec
