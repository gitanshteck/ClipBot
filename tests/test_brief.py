"""The stream brief: clipbot/brief.py (validation, page rendering, live state,
markers, feedback) and its dashboard routes (/brief, the OBS overlays and dock,
/api/brief/*).

The server tests use FastAPI's test client without its context manager, so the
startup hook (job threads, the brief watcher) never runs.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipbot import brief
from clipbot.config import Settings

try:
    from fastapi.testclient import TestClient
    from clipbot.server import app as app_module
except Exception:  # pragma: no cover - server extras not installed
    TestClient = None

HEADERS = {"X-ClipBot": "1"}


def sample_brief(**over):
    b = {
        "schema": 2, "date": "2026-11-14", "weekday": "Saturday", "edition": 3,
        "generated_at": "2026-11-14T16:30+11:00",
        "headline": {"kicker": "k", "line": "talk first", "why": "because"},
        "go_live": {"start_aedt": "7:30 pm", "start_ist": "2:00 pm", "titles": ["t"], "checklist": ["c"]},
        "platforms": [{"name": "YouTube", "role": "Home", "action": "a", "why": "w"}],
        "run": [
            {"id": "starting", "t": -5, "dur": 5, "name": "Starting soon", "kind": "starting", "clip": False, "scene": "starting",
             "screen": {"title": "tonight", "sub": "", "prompt": "say hi"}},
            {"id": "react", "t": 0, "dur": 10, "name": "React", "kind": "react", "clip": True, "scene": "topic", "items": ["d1"],
             "screen": {"title": "news", "sub": "", "prompt": "thoughts?"}},
            {"id": "switch", "t": 10, "dur": 5, "name": "Pick", "kind": "transition", "clip": False, "scene": "vote",
             "poll": ["a", "b"], "screen": {"title": "pick", "sub": "", "prompt": "1 or 2"}},
            {"id": "game", "t": 15, "dur": 120, "name": "Game", "kind": "game", "clip": True, "scene": "game",
             "screen": {"title": "warzone", "sub": "", "prompt": "o/u"}},
        ],
        "docket": [{"id": "d1", "block": "News", "headline": "A headline </script><b>", "card": "short card",
                    "summary": "s", "take": "t", "question": "q", "clip": "high", "risk": "r", "source": "src",
                    "url": "https://example.com", "date": "14 Nov"}],
        "keep_alive": [], "clip_targets": [], "games": {"ranked": [{"game": "X", "appid": "10"}]},
        "week": [], "calendar": [], "evidence": {}, "sources": [],
    }
    b.update(over)
    return b


class BriefTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.work.mkdir()
        self.settings = Settings({"work_root": str(self.work), "server": {"host": "127.0.0.1", "port": 8765}}, Path("settings.json"))
        self.dir = brief.brief_dir(self.settings)

    def write_brief(self, data=None, ccu=None):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "brief.json").write_text(json.dumps(data or sample_brief()), encoding="utf-8")
        if ccu is not None:
            (self.dir / "steam_ccu.json").write_text(json.dumps(ccu), encoding="utf-8")


class TestBriefData(BriefTestCase):
    def test_brief_dir_defaults_under_work_root_and_honours_setting(self):
        self.assertEqual(self.dir, self.work / "_brief")
        other = Path(self._tmp.name) / "elsewhere"
        s = Settings({"work_root": str(self.work), "brief": {"dir": str(other)}}, Path("settings.json"))
        self.assertEqual(brief.brief_dir(s), other)

    def test_a_complete_brief_has_no_problems(self):
        self.assertEqual(brief.problems(sample_brief()), [])

    def test_problems_name_what_is_wrong(self):
        b = sample_brief()
        b["run"][1]["items"] = ["missing"]
        b["run"][2]["poll"] = ["only one"]
        b["docket"][0]["card"] = "x" * 60
        b["run"].append({"id": "g", "t": 1, "dur": 1, "name": "n", "kind": "talk", "clip": False, "scene": "nope",
                         "screen": {"title": "", "sub": "", "prompt": ""}})
        text = "\n".join(brief.problems(b))
        self.assertIn("'missing' that does not exist", text)
        self.assertIn("vote scene needs a poll", text)
        self.assertIn("card title over 48", text)
        self.assertIn("scene 'nope'", text)

    def test_private_words_are_refused(self):
        b = sample_brief()
        b["headline"]["why"] = "my visa ends soon"
        self.assertTrue(any("private-looking" in p for p in brief.problems(b)))

    def test_load_brief_merges_steam_players_by_appid(self):
        self.write_brief(ccu={"fetched_at": "now", "games": {"X": {"appid": "10", "players": 1234}}})
        data = brief.load_brief(self.settings)
        self.assertEqual(data["games"]["ranked"][0]["steam_players"], 1234)
        self.assertEqual(data["games"]["steam_fetched_at"], "now")

    def test_load_brief_without_a_brief_raises(self):
        with self.assertRaises(FileNotFoundError):
            brief.load_brief(self.settings)

    def test_render_page_inlines_code_and_data_safely(self):
        html = brief.render_page(sample_brief(), "local", "http://127.0.0.1:9000")
        for marker in ("__BRIEF_JSON__", "/*__SCENES_CSS__*/", "/*__SCENES_JS__*/", "/*__LIVE_JS__*/"):
            self.assertNotIn(marker, html)
        self.assertIn("window.BriefScenes", html)
        self.assertIn('"backend": "local"', html)
        self.assertIn("http://127.0.0.1:9000", html)
        # a headline holding </script> must not end the data block early
        self.assertNotIn("</script><b>", html)
        with self.assertRaises(ValueError):
            brief.render_page(sample_brief(), "cloud")


class TestLiveState(BriefTestCase):
    def state(self, **over):
        s = brief.default_state("2026-11-14")
        s.update({"v": 100, "on": True, "seg": 1})
        s.update(over)
        return s

    def test_newer_state_wins_and_older_is_ignored(self):
        stored = brief.write_state(self.settings, self.state(v=200, seg=2))
        self.assertEqual(stored["seg"], 2)
        stale = brief.write_state(self.settings, self.state(v=150, seg=1))
        self.assertEqual(stale["seg"], 2)
        self.assertEqual(brief.read_state(self.settings, "2026-11-14")["v"], 200)

    def test_a_new_day_replaces_yesterdays_state(self):
        brief.write_state(self.settings, self.state(v=999, date="2026-11-13"))
        stored = brief.write_state(self.settings, self.state(v=1, date="2026-11-14"))
        self.assertEqual(stored["date"], "2026-11-14")
        self.assertEqual(brief.read_state(self.settings, "2026-11-15")["v"], 0)

    def test_bad_state_is_refused(self):
        for bad in ({"date": "2026-11-14"}, self.state(seg=-1), self.state(bg="pink"), self.state(lay="x"),
                    self.state(kills="3"), self.state(junk="x" * 9000)):
            with self.assertRaises(ValueError):
                brief.write_state(self.settings, bad)

    def test_unknown_keys_are_dropped(self):
        stored = brief.write_state(self.settings, self.state(extra="nope"))
        self.assertNotIn("extra", stored)


class TestMarkersAndFeedback(BriefTestCase):
    def test_markers_append_per_day(self):
        brief.add_marker(self.settings, {"date": "2026-11-14", "kind": "segment", "seg_id": "react", "label": "news"})
        brief.add_marker(self.settings, {"date": "2026-11-14", "kind": "clip", "seg_id": "react", "label": "x" * 500})
        data = brief.read_markers(self.settings, "2026-11-14")
        self.assertEqual([m["kind"] for m in data["markers"]], ["segment", "clip"])
        self.assertEqual(len(data["markers"][1]["label"]), 200)
        self.assertTrue(data["markers"][0]["at"].endswith("+00:00"))

    def test_bad_markers_are_refused(self):
        for bad in ({"date": "2026-11-14", "kind": "boom"}, {"date": "14/11/2026", "kind": "clip"}, "nope"):
            with self.assertRaises(ValueError):
                brief.add_marker(self.settings, bad)

    def test_feedback_set_replace_and_clear(self):
        row = {"date": "2026-11-14", "kind": "docket", "target": "d1", "label": "card", "reaction": "more"}
        brief.set_feedback(self.settings, row)
        brief.set_feedback(self.settings, dict(row, reaction="drop"))
        rows = brief.list_feedback(self.settings, "2026-11-14")
        self.assertEqual([(r["target"], r["reaction"]) for r in rows], [("d1", "drop")])
        brief.set_feedback(self.settings, dict(row, reaction=None))
        self.assertEqual(brief.list_feedback(self.settings), [])

    def test_bad_feedback_is_refused(self):
        with self.assertRaises(ValueError):
            brief.set_feedback(self.settings, {"date": "2026-11-14", "kind": "docket", "target": "d1", "reaction": "love"})
        with self.assertRaises(ValueError):
            brief.set_feedback(self.settings, {"date": "2026-11-14", "kind": "weather", "target": "d1", "reaction": "more"})


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestBriefRoutes(BriefTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(app_module, "settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")

    def test_pages_without_a_brief(self):
        self.assertEqual(self.client.get("/brief").status_code, 404)
        self.assertEqual(self.client.get("/api/brief").status_code, 404)
        # overlays and the dock still load, and wait for a brief
        self.assertEqual(self.client.get("/brief/overlay/chat").status_code, 200)
        self.assertEqual(self.client.get("/brief/dock").status_code, 200)

    def test_rundown_overlay_and_dock_pages(self):
        self.write_brief()
        page = self.client.get("/brief")
        self.assertEqual(page.status_code, 200)
        self.assertIn('"backend": "local"', page.text)
        for frame in brief.FRAMES:
            r = self.client.get("/brief/overlay/" + frame)
            self.assertEqual(r.status_code, 200)
            self.assertIn('data-frame="{0}"'.format(frame), r.text)
        self.assertIn('data-bg="brand"', self.client.get("/brief/overlay/full").text)
        self.assertIn('data-bg="clear"', self.client.get("/brief/overlay/chat").text)
        self.assertIn('data-bg="green"', self.client.get("/brief/overlay/chat?bg=green").text)
        self.assertEqual(self.client.get("/brief/overlay/sideways").status_code, 404)
        self.assertEqual(self.client.get("/brief/overlay/chat?bg=pink").status_code, 400)
        self.assertIn("/brief/overlay/game", self.client.get("/brief/dock").text)

    def test_state_needs_the_header_and_keeps_the_newest(self):
        self.write_brief()
        st = brief.default_state("2026-11-14")
        st.update({"v": 50, "on": True, "seg": 2})
        self.assertEqual(self.client.put("/api/brief/state", json=st).status_code, 403)
        r = self.client.put("/api/brief/state", json=st, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/brief/state").json()["seg"], 2)
        bad = dict(st, bg="pink", v=60)
        self.assertEqual(self.client.put("/api/brief/state", json=bad, headers=HEADERS).status_code, 400)

    def test_state_from_another_day_reads_as_fresh(self):
        self.write_brief()
        st = brief.default_state("2026-11-13")
        st.update({"v": 50, "on": True})
        self.client.put("/api/brief/state", json=st, headers=HEADERS)
        self.assertEqual(self.client.get("/api/brief/state").json()["v"], 0)

    def test_markers_and_feedback_round_trip(self):
        self.write_brief()
        r = self.client.post("/api/brief/markers", json={"date": "2026-11-14", "kind": "clip", "seg_id": "react"}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.client.get("/api/brief/markers?date=2026-11-14").json()["markers"]), 1)
        self.assertEqual(self.client.post("/api/brief/markers", json={"date": "x", "kind": "clip"}, headers=HEADERS).status_code, 400)
        row = {"date": "2026-11-14", "kind": "scene", "target": "s1", "reaction": "less"}
        self.assertEqual(self.client.post("/api/brief/feedback", json=row, headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.get("/api/brief/feedback?date=2026-11-14").json()["feedback"][0]["reaction"], "less")


if __name__ == "__main__":
    unittest.main()
