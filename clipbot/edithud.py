"""The HUD look of an episode (`lineup["look"] == "hud"`): AFTER ACTION, a tactical debrief.

Where the plain roundup draws a lower-third and a callout (stills, `editoverlay`), the HUD look draws motion graphics
(`graphics/`, `editmotion`): a series card that slams in, an operation header over the first seconds of a chapter, a
contact report on every kill (the range counting up to a lock on a 0-300 m ruler) and a debrief card of what the
video contained. This module only builds the overlay dicts and the card text from the lineup's data; it draws
nothing and knows no clip, so `edittemplates.episode` calls it where it would otherwise build the stills.

Every number a graphic shows is a number the lineup carries (the feed's distance, the kill count of what is in the
edit), never one made up for the look. A kill whose distance the feed did not give is shown as a plain
KILL CONFIRMED plate, not with a guessed range.
"""

from typing import Any, Dict, List, Optional

ACCENT = "#FFB300"

CONTACT_DUR = 3.2
CONTACT_HIT = 0.55
HERO_DUR = 4.6
HERO_HIT = 0.75
OP_DUR = 2.6
OP_HIT = 0.5
OP_LANDING = 0.9              # seconds into the chapter's first clip at which the header lands (after any transition)
CONTACT_STACK = 372           # px between two plates on screen at once
MULTI_WINDOW = 4.0            # kills this many seconds apart are a multi-kill (DOUBLE KILL...); further apart, a run
TITLE_DUR = 3.6
TITLE_HIT = 1.0
STATS_DUR = 7.0
STATS_HIT = 0.6

TONES = {"kill": "kill", "headshot": "head", "nade": "nade", "multi": "multi", "long": "long"}


def tags_for(kill: Dict[str, Any]) -> List[str]:
    out = []
    if kill.get("headshot"):
        out.append("HEADSHOT")
    if kill.get("weapon") == "grenade":
        out.append("NADE")
    if kill.get("through_cover"):
        out.append("THROUGH COVER")
    return out


def contact(kill: Dict[str, Any], headline: str, accent_key: str, index: int, total: int, y_px: Optional[int] = None,
            hero: bool = False, note: Optional[str] = None) -> Dict[str, Any]:
    """The contact plate for one kill. `headline`/`accent_key` come from `edittemplates._label` (so DOUBLE KILL builds
    as the clip plays). The graphic lands on the kill: `at` is the kill's source second."""
    # the plate already says the range, and HEADSHOT / NADE are chips: the headline only adds a multi-kill's count
    props: Dict[str, Any] = {"index": index, "total": total, "headline": headline if accent_key == "multi" else "KILL CONFIRMED",
                             "tone": TONES.get(accent_key, "kill"), "tags": tags_for(kill)}
    d = kill.get("dist_m")
    if d is not None:
        props["distance"] = int(round(d))
    victim = (kill.get("victim") or "").strip()
    if victim and any(ch.isalpha() for ch in victim):          # a name that is all digits is OCR noise, not a name
        props["victim"] = victim
    if kill.get("icon"):
        props["icon"] = "file:" + str(kill["icon"]).replace("\\", "/")
    if y_px is not None:
        props["y"] = int(y_px)
    if hero:
        props["hero"] = True
        if note:
            props["tags"] = list(props["tags"]) + [note]
    return {"kind": "motion", "comp": "Contact", "at": round(float(kill["t"]), 3), "dur": HERO_DUR if hero else CONTACT_DUR,
            "hit": HERO_HIT if hero else CONTACT_HIT, "props": props}


def op_title(op: int, title: str, sub: str, at: float, label: str = "OPERATION") -> Dict[str, Any]:
    props: Dict[str, Any] = {"op": op, "title": title.upper(), "label": label}
    if sub:
        props["sub"] = sub.upper()
    return {"kind": "motion", "comp": "OpTitle", "at": round(at, 3), "dur": OP_DUR, "hit": OP_HIT, "props": props}


def op_sub(kills: List[Dict[str, Any]]) -> str:
    """'3 CONTACTS // LONGEST 99 M' from the kills of a chapter (the longest only when the feed gave distances)."""
    if not kills:
        return ""
    n = len(kills)
    s = "{0} CONTACT{1}".format(n, "" if n == 1 else "S")
    far = [k["dist_m"] for k in kills if k.get("dist_m") is not None]
    if far:
        s += "  //  LONGEST {0:.0f} M".format(max(far))
    return s


def title_card(series: str, number: Optional[int], kicker: str, sub: str, dur: float = TITLE_DUR,
               meta: Optional[List[str]] = None) -> Dict[str, Any]:
    """`meta` is the typed column in the card's corner (STREAMS 03 ...): counts of what the report holds."""
    props: Dict[str, Any] = {"series": series.upper(), "kicker": kicker.upper(), "sub": sub.upper()}
    if number is not None:
        props["number"] = int(number)
    if meta:
        props["meta"] = [m.upper() for m in meta]
    return {"id": "sting", "type": "card", "dur": dur, "bg": "#06080A", "label": "Intro", "tags": ["nochapter"],
            "overlays": [{"kind": "motion", "comp": "Title", "at": TITLE_HIT, "dur": dur, "hit": TITLE_HIT, "props": props}]}


def tally(kills: List[Dict[str, Any]], streams: int) -> Dict[str, Any]:
    """What the edit contains, for the debrief: counts of the kills that are in it (not of those that were considered)."""
    dists = [k["dist_m"] for k in kills if k.get("dist_m") is not None]
    return {
        "kills": len(kills),
        "headshots": sum(1 for k in kills if k.get("headshot")),
        "nades": sum(1 for k in kills if k.get("weapon") == "grenade"),
        "far": max(dists) if dists else None,
        "avg": (sum(dists) / len(dists)) if dists else None,
        "measured": len(dists),
        "streams": streams,
    }


def stats_card(series: str, number: Optional[int], t: Dict[str, Any], ops: List[Dict[str, Any]], dur: float = STATS_DUR,
               title: str = "MISSION COMPLETE", next_label: str = "NEXT REPORT") -> Dict[str, Any]:
    stats = [{"label": "KILLS", "value": t["kills"]}]
    if t.get("far") is not None:
        stats.append({"label": "LONGEST SHOT", "value": int(round(t["far"])), "unit": "M"})
    stats.append({"label": "HEADSHOTS", "value": t["headshots"]})
    stats.append({"label": "GRENADES", "value": t["nades"]})
    if t.get("avg") is not None:
        stats.append({"label": "AVG RANGE", "value": int(round(t["avg"])), "unit": "M"})
    stats.append({"label": "STREAMS", "value": t["streams"]})
    foot = series.upper() + ("  //  REPORT {0:02d}".format(int(number)) if number is not None else "")
    props = {"title": title, "foot": foot, "stats": stats, "ops": ops, "next": next_label}
    return {"id": "end", "type": "card", "dur": dur, "bg": "#06080A", "label": "End card", "tags": ["nochapter"],
            "overlays": [{"kind": "motion", "comp": "Stats", "at": STATS_HIT, "dur": dur, "hit": STATS_HIT, "props": props}]}
