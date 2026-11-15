"""Lore: the private pack the on-stream AI reads (clipbot/lore.py) and its use in the chat prompt."""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from clipbot import briefchat, lore
from clipbot.config import Settings

try:
    from fastapi.testclient import TestClient
    from clipbot.server import app as app_module
except Exception:  # pragma: no cover
    TestClient = None
    app_module = None

STREAMS = [
    {"date": "2026-11-13", "title": "ONETHREE giveaway", "games": ["GTA V"], "topics": ["hoodie giveaway", "gta 6 console"],
     "moments": [{"t_min": 36, "what": "Typed-entry giveaway pulls 223 chat messages in ten minutes", "kind": "giveaway"}],
     "lines": ["arre bhai the hoodie winner gets it this week"]},
    {"date": "2026-10-12", "title": "Dinner and a video", "games": [], "topics": ["youtube strategy"],
     "moments": [{"t_min": 55, "what": "Reacts to a Vsauce style video about youtube being solved", "kind": "take"}], "lines": []},
]


def make(tmp, **cfg):
    work = Path(tmp) / "work"
    work.mkdir(exist_ok=True)
    return Settings(dict({"work_root": str(work)}, **cfg), Path("settings.json"))


def write_pack(settings, lore_md="", recent_md="", streams=None):
    d = lore.lore_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    if lore_md:
        (d / "lore.md").write_text(lore_md, encoding="utf-8")
    if recent_md:
        (d / "recent.md").write_text(recent_md, encoding="utf-8")
    if streams is not None:
        (d / "streams.json").write_text(json.dumps(streams), encoding="utf-8")


class TestLore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = make(self._tmp.name)

    def test_no_pack_means_no_block(self):
        self.assertEqual(lore.context(self.settings, "what happened on the 13th?"), "")
        self.assertEqual(lore.load_streams(self.settings), [])

    def test_the_block_carries_lore_recent_and_the_never_say_rule(self):
        write_pack(self.settings, "## Running bits\n- parrot guessing", "13 Nov: giveaway night")
        block = lore.context(self.settings, "")
        self.assertIn("parrot guessing", block)
        self.assertIn("13 Nov: giveaway night", block)
        self.assertIn("Never state on stream", block)

    def test_can_be_switched_off(self):
        write_pack(self.settings, "secret lore")
        off = make(self._tmp.name, brief={"chat": {"lore": False}})
        self.assertEqual(lore.context(off, "anything"), "")

    def test_lore_is_capped_on_a_whole_line(self):
        long = "\n".join("line %03d %s" % (i, "x" * 60) for i in range(400))
        write_pack(self.settings, long)
        block = lore.context(self.settings, "")
        self.assertLess(len(block), lore.LORE_MAX_CHARS + 900)
        self.assertNotIn("line 399", block)
        self.assertTrue(block.rstrip().endswith("x" * 60))   # no half line

    def test_search_finds_the_matching_moment_and_ignores_noise(self):
        hits = lore.search(STREAMS, "when was that hoodie giveaway stream?")
        self.assertEqual(hits[0]["date"], "2026-11-13")
        self.assertEqual(lore.search(STREAMS, "what is the weather like"), [])
        self.assertEqual(lore.search(STREAMS, "the and of"), [])      # only stopwords

    def test_search_prefers_the_newest_stream_on_a_tie(self):
        twin = [dict(STREAMS[0], date="2026-10-01"), dict(STREAMS[0], date="2026-11-20")]
        self.assertEqual(lore.search(twin, "hoodie giveaway")[0]["date"], "2026-10-01")  # file order is newest first: the first row wins
        self.assertEqual(lore.search(list(reversed(twin)), "hoodie giveaway")[0]["date"], "2026-11-20")

    def test_matching_moments_are_added_for_the_question(self):
        write_pack(self.settings, "lore", streams=STREAMS)
        block = lore.context(self.settings, "what did I say about the youtube strategy video?")
        self.assertIn("Past moments that match", block)
        self.assertIn("2026-10-12", block)
        self.assertIn("at ~55 min", block)

    def test_a_broken_streams_file_is_not_an_error(self):
        d = lore.lore_dir(self.settings)
        d.mkdir(parents=True)
        (d / "streams.json").write_text("{not json", encoding="utf-8")
        write_pack(self.settings, "lore")
        self.assertIn("lore", lore.context(self.settings, "hoodie"))


class TestPromptBackground(unittest.TestCase):
    def test_empty_background_leaves_the_prompt_unchanged(self):
        before = briefchat.build_prompt("q", [], "CTX", True)
        self.assertEqual(briefchat.build_prompt("q", [], "CTX", True, ""), before)
        self.assertNotIn("Background about the streamer", before)

    def test_background_goes_between_context_and_the_question(self):
        p = briefchat.build_prompt("the question", [], "CTX", False, "BACKGROUND BLOCK")
        self.assertTrue(p.index("CTX") < p.index("BACKGROUND BLOCK") < p.index("Question: the question"))


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestRoute(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = make(self._tmp.name)
        p = mock.patch.object(app_module, "settings", self.settings)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")

    def ask(self):
        captured = {}

        def fake_ask(settings, ask_obj, prompt, on_delta):
            captured["prompt"] = prompt
            ask_obj.text = "ok"
            ask_obj.done = True
            return ask_obj

        class Sync(object):
            def __init__(self, target=None, args=(), daemon=None, **kw):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with mock.patch.object(briefchat, "pick_provider", return_value="anthropic"), \
                mock.patch.object(briefchat, "ask", side_effect=fake_ask), \
                mock.patch.object(app_module, "threading", types.SimpleNamespace(Thread=Sync)):
            r = self.client.post("/api/brief/chat", json={"question": "tell me about the hoodie giveaway"}, headers={"X-ClipBot": "1"})
        return r, captured

    def test_the_lore_reaches_the_prompt(self):
        write_pack(self.settings, "RUNNING BIT: parrot guessing", streams=STREAMS)
        r, captured = self.ask()
        self.assertEqual(r.status_code, 200)
        self.assertIn("RUNNING BIT: parrot guessing", captured["prompt"])
        self.assertIn("2026-11-13", captured["prompt"])

    def test_without_a_pack_the_route_works_as_before(self):
        r, captured = self.ask()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Background about the streamer", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
