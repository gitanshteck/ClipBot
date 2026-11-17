"""The `hl` look of an episode (`lineup["look"] == "hl"`): TECK HIGHLIGHTS, a gaming-channel reel.

Where the plain roundup draws a lower-third and a callout (stills, `editoverlay`) and the HUD look (`edithud`) draws a
tactical debrief, this look draws the channel's own graphics as motion (`graphics/`, `editmotion`): a series card that
slams in, a numbered chapter header over the first seconds of a chapter, a plate on every kill in the game's own words
(the line it printed and the medals it stacked under it), a pop-in tag for a gag or a quote, and an end card of the
night's numbers. This module only builds the overlay dicts and the card text from the lineup's data; it draws nothing
and knows no clip, so `edittemplates.episode` calls it where it would otherwise build the stills.

Every word and number a graphic shows is one the data carries: the kill plate repeats what the game's popup said
(`killfeed.json` `popup`, read by `stages/killfeed.py`), the counts are counts of the kills that are in the edit, and a
kill with no popup text gets the plain KILL / HEADSHOT KILL the feed itself implies. Nothing is made up for the look.
"""

from typing import Any, Dict, List, Optional

KILL_DUR = 3.0
KILL_HIT = 0.3
KILL_MIN_DUR = 1.0            # a plate cut short by the next kill never lives less than this
KILL_BOTTOM = 90              # px from the bottom edge to the bottom of a kill plate's stack (it grows upward)
CHAPTER_DUR = 2.8
CHAPTER_HIT = 0.45
CHAPTER_LANDING = 0.6         # seconds into the chapter's first clip at which the header lands (after any transition)
TAG_DUR = 2.4
TAG_HIT = 0.3
TITLE_DUR = 3.2
TITLE_HIT = 0.9
END_DUR = 4.0
END_HIT = 0.5
MAX_TAGS = 3
MAX_STATS = 4
BELOW_CHAPTER_Y = 300         # where a tag goes when it shares a clip with a chapter header

# What the game's popup says under the kill line, most worth showing first. A kill keeps at most MAX_TAGS of them.
TAG_ORDER = ("LONGSHOT", "QUICK SCOPE", "ONE SHOT, ONE KILL", "WALL BANG", "POINT BLANK", "LOW BLOW", "KINGSLAYER",
             "AVENGER", "REVENGE", "SAVIOR", "BUZZKILL", "TRADE", "SURVIVOR", "BACKFIRE", "BLOODTHIRSTY", "GROUNDED")
MULTI_NAMES = {2: "DOUBLE KILL", 3: "TRIPLE KILL", 4: "QUAD KILL", 5: "PENTA KILL"}


def popup_tags(popup: Optional[Dict[str, Any]]) -> List[str]:
    """The medal lines of one kill's popup, best first, at most MAX_TAGS. `objective` (OFFENSE / DEFENSE, the reader's
    weakest call) and `support` lines are left out: the plate is about the kill."""
    if not popup:
        return []
    seen: List[str] = []
    for name in list(popup.get("medals") or []) + list(popup.get("situations") or []):
        if name and name not in seen:
            seen.append(name)
    seen.sort(key=lambda n: TAG_ORDER.index(n) if n in TAG_ORDER else len(TAG_ORDER))
    return seen[:MAX_TAGS]


def kill_kind(kill: Dict[str, Any]) -> str:
    """The line the game printed for the kill. When the popup was read but its kind line was not (the medals under it
    were), the plate says plain KILL - always true - rather than trusting the feed's skull, which once called a plain KILL
    a HEADSHOT KILL (k007). Only a kill with no popup at all falls back on what the feed implies."""
    popup = kill.get("popup")
    if popup:
        return str(popup.get("kind") or "KILL")
    return "HEADSHOT KILL" if kill.get("headshot") else "KILL"


def kill_plate(kill: Dict[str, Any], index: int, total: int, chain: int = 1, dur: float = KILL_DUR) -> Dict[str, Any]:
    """The plate for one kill. The graphic lands on the kill: `at` is the kill's source second. `chain` is this kill's
    place in a run of kills a few seconds apart; the game's own multi-kill line wins over a count made from `chain`."""
    popup = kill.get("popup") or {}
    props: Dict[str, Any] = {"kind": kill_kind(kill), "index": int(index), "total": int(total), "tags": popup_tags(popup)}
    multi = popup.get("multi") or (MULTI_NAMES.get(chain) if not popup and chain >= 2 else None)
    if multi:
        props["multi"] = str(multi)
    return {"kind": "motion", "comp": "HlKill", "at": round(float(kill["t"]), 3), "dur": round(float(dur), 3),
            "hit": KILL_HIT, "props": props}


def plate_durations(times: List[float]) -> List[float]:
    """How long each plate of a clip's kills stays up: the full life, or less when the next kill comes sooner (a plate
    is gone before the next one lands rather than stacked under it)."""
    out = []
    for i, t in enumerate(times):
        if i + 1 < len(times):
            out.append(max(KILL_MIN_DUR, min(KILL_DUR, times[i + 1] - t - 0.05)))
        else:
            out.append(KILL_DUR)
    return out


def chapter_header(n: int, title: str, sub: str, at: float) -> Dict[str, Any]:
    props: Dict[str, Any] = {"n": int(n), "title": title.upper()}
    if sub:
        props["sub"] = sub.upper()
    return {"kind": "motion", "comp": "HlChapter", "at": round(at, 3), "dur": CHAPTER_DUR, "hit": CHAPTER_HIT, "props": props}


def chapter_sub(kills: List[Dict[str, Any]]) -> str:
    """'4 KILLS' from the kills of a chapter; nothing for a chapter of talk."""
    if not kills:
        return ""
    return "{0} KILL{1}".format(len(kills), "" if len(kills) == 1 else "S")


def tag(text: str, at: float, sub: str = "", tone: str = "cyan", y: Optional[int] = None, dur: float = TAG_DUR,
        align: Optional[str] = None) -> Dict[str, Any]:
    props: Dict[str, Any] = {"text": text.upper(), "tone": tone}
    if sub:
        props["sub"] = sub.upper()
    if y is not None:
        props["y"] = int(y)
    if align:
        props["align"] = align
    return {"kind": "motion", "comp": "HlTag", "at": round(at, 3), "dur": dur, "hit": TAG_HIT, "props": props}


def title_card(series: str, kicker: str, sub: str, tag_text: str = "", dur: float = TITLE_DUR) -> Dict[str, Any]:
    props: Dict[str, Any] = {"series": series.upper(), "kicker": kicker.upper(), "sub": sub.upper()}
    if tag_text:
        props["tag"] = tag_text.upper()
    return {"id": "sting", "type": "card", "dur": dur, "bg": "#06080C", "label": "Intro", "tags": ["nochapter"],
            "overlays": [{"kind": "motion", "comp": "HlTitle", "at": TITLE_HIT, "dur": dur, "hit": TITLE_HIT, "props": props}]}


def tally(kills: List[Dict[str, Any]]) -> Dict[str, int]:
    """What the edit contains, from the kills that are in it (not of those that were considered)."""
    medals = [m for k in kills for m in (k.get("popup") or {}).get("medals", [])]
    return {
        "kills": len(kills),
        "headshots": sum(1 for k in kills if k.get("headshot") or "HEADSHOT" in kill_kind(k)),     # the feed's skull or the popup's words
        "quickscopes": sum(1 for m in medals if m == "QUICK SCOPE"),
        "longshots": sum(1 for m in medals if m == "LONGSHOT"),
    }


def night(doc: Dict[str, Any]) -> Dict[str, int]:
    """The night's own numbers from one `killfeed.json`: for an end card that says what the whole stream held."""
    events = doc.get("events", [])
    return tally([{"popup": e.get("popup"), "headshot": e.get("headshot")} for e in events])


def end_card(stats: List[Dict[str, Any]], line: str, foot: str = "", title: str = "THE NIGHT IN NUMBERS",
             dur: float = END_DUR) -> Dict[str, Any]:
    props: Dict[str, Any] = {"title": title.upper(), "stats": stats[:MAX_STATS], "line": line.upper()}
    if foot:
        props["foot"] = foot.upper()
    return {"id": "end", "type": "card", "dur": dur, "bg": "#06080C", "label": "End card", "tags": ["nochapter"],
            "overlays": [{"kind": "motion", "comp": "HlEnd", "at": END_HIT, "dur": dur, "hit": END_HIT, "props": props}]}


def default_stats(t: Dict[str, int]) -> List[Dict[str, Any]]:
    """The tiles for an end card when the lineup does not give its own: only counts that are not zero."""
    rows = [("KILLS", t["kills"]), ("HEADSHOTS", t["headshots"]), ("QUICK SCOPES", t["quickscopes"]), ("LONGSHOTS", t["longshots"])]
    return [{"label": label, "value": value} for label, value in rows if value > 0][:MAX_STATS]
