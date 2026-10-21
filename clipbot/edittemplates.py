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
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from . import editspec, killfeed

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
        out.append({"src": alias, "t": float(e["t"]), "dist_m": e.get("dist_m"), "weapon": e.get("weapon", "gun"),
                    "headshot": bool(e.get("headshot")), "through_cover": bool(e.get("through_cover")),
                    "victim": killfeed.readable_name(e.get("victim") or ""), "icon": icon})
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
        markers, overlays, camera, fx = [], [], [], []
        speed: List[Dict[str, Any]] = []
        best = max(s["kills"], key=kill_score)
        punches = 0
        for i, k in enumerate(s["kills"]):
            t = k["t"]
            head, sub, accent = _label(k, i + 1)
            markers.append({"at": round(t, 3), "name": "kill"})
            ov: Dict[str, Any] = {"kind": "callout", "at": round(t, 3), "dur": 2.2, "text": head, "accent": ACCENTS[accent],
                                  "x": callout_xy[0], "y": callout_xy[1], "size": 0.050, "anim_in": "pop", "anim_out": "fade"}
            if sub:
                ov["sub"] = sub
            if k.get("icon"):
                ov["image"] = k["icon"]
            if i > 0 and t - s["kills"][i - 1]["t"] < 2.4:  # two callouts must not overlap on screen
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
        want_slow = (slowmo == "all") or (slowmo == "nade" and best.get("weapon") == "grenade") or \
                    (slowmo == "special" and (best.get("weapon") == "grenade" or best.get("headshot") or (best.get("dist_m") or 0) >= 100))
        if want_slow:
            speed = _slowmo_keys(best["t"], lo, hi, prof["slowmo_rate"])
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
