"""The stream brief's live chat: clipbot/briefchat.py and its /api/brief/chat* routes.

The CLI runner is tested against a real subprocess (this Python); the Anthropic
runner against a fake ``anthropic`` module. Nothing here reaches a model.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from clipbot import brief, briefchat
from clipbot.config import Settings

from test_brief import sample_brief

try:
    from fastapi.testclient import TestClient
    from clipbot.server import app as app_module
except Exception:  # pragma: no cover - server extras not installed
    TestClient = None

HEADERS = {"X-ClipBot": "1"}


def make_settings(work, **chat):
    data = {"work_root": str(work), "server": {"host": "127.0.0.1", "port": 8765}}
    if chat:
        data["brief"] = {"chat": chat}
    return Settings(data, Path("settings.json"))


class TestParseAnswer(unittest.TestCase):
    def test_head_and_points(self):
        r = briefchat.parse_answer("Yes, it is.\n- one\n- two\n- three")
        self.assertEqual(r, {"head": "Yes, it is.", "points": ["one", "two", "three"]})

    def test_extra_points_dropped(self):
        r = briefchat.parse_answer("H\n- 1\n- 2\n- 3\n- 4\n- 5")
        self.assertEqual(r["points"], ["1", "2", "3"])

    def test_other_bullets(self):
        r = briefchat.parse_answer("H\n* star\n• dot")
        self.assertEqual(r["points"], ["star", "dot"])

    def test_heading_marks_stripped(self):
        self.assertEqual(briefchat.parse_answer("## Big answer\n- a")["head"], "Big answer")

    def test_multi_line_head_joined(self):
        r = briefchat.parse_answer("first part\nsecond part\n- a\nlate line")
        self.assertEqual(r["head"], "first part second part")
        self.assertEqual(r["points"], ["a"])

    def test_empty(self):
        for text in ("", None, "  \n\r\n "):
            self.assertEqual(briefchat.parse_answer(text), {"head": "", "points": []})


class TestBuildPrompt(unittest.TestCase):
    def test_contains_rules_context_and_question(self):
        p = briefchat.build_prompt("what is a pog?", [], "CTX LINE")
        self.assertIn(briefchat.SYSTEM, p)
        self.assertIn("CTX LINE", p)
        self.assertIn("Question: what is a pog?", p)
        self.assertNotIn("Earlier in this chat", p)

    def test_keeps_only_the_last_turns(self):
        hist = [{"q": "q%d" % i, "a": "a%d" % i} for i in range(10)]
        p = briefchat.build_prompt("now", hist, "c")
        self.assertIn("Earlier in this chat", p)
        for i in range(10 - briefchat.HISTORY_TURNS):
            self.assertNotIn("Q: q%d\n" % i, p + "\n")
        for i in range(10 - briefchat.HISTORY_TURNS, 10):
            self.assertIn("Q: q%d" % i, p)
            self.assertIn("A: a%d" % i, p)

    def test_long_question_truncated(self):
        p = briefchat.build_prompt("x" * (briefchat.QUESTION_MAX + 200), [], "c")
        self.assertIn("Question: " + "x" * briefchat.QUESTION_MAX, p)
        self.assertNotIn("x" * (briefchat.QUESTION_MAX + 1), p)


class TestStreamContext(unittest.TestCase):
    def state(self, **over):
        s = brief.default_state("2026-11-14")
        s.update(over)
        return s

    def test_no_brief(self):
        text = briefchat.stream_context(None, None, "the dock")
        self.assertIn("LIVE", text)
        self.assertIn("the dock", text)
        self.assertNotIn("Tonight's theme", text)

    def test_react_segment_shows_docket_item(self):
        text = briefchat.stream_context(sample_brief(), self.state(seg=1))
        self.assertIn("Tonight's theme: k", text)
        self.assertIn("segment 'React'", text)
        self.assertIn("A headline", text)
        self.assertIn("src", text)

    def test_guess_round_hides_the_answer(self):
        b = sample_brief()
        b["run"][1]["items"] = []
        b["run"][1]["rounds"] = [{"q": "Which game is this?", "a": "SECRETANSWER"}]
        text = briefchat.stream_context(b, self.state(seg=1))
        self.assertIn("Which game is this?", text)
        self.assertNotIn("SECRETANSWER", text)

    def test_game_segment_shows_kills_and_deaths(self):
        text = briefchat.stream_context(sample_brief(), self.state(seg=3, kills=7, deaths=2))
        self.assertIn("Kills 7, deaths 2", text)

    def test_state_from_another_day(self):
        s = self.state(seg=1)
        s["date"] = "2026-11-13"
        text = briefchat.stream_context(sample_brief(), s)
        self.assertIn("has not started", text)
        self.assertNotIn("On screen", text)


class TestProviders(unittest.TestCase):
    def setUp(self):
        self.settings = Settings({}, Path("settings.json"))
        p = mock.patch.object(briefchat, "_have_anthropic_profile", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, tools, env, provider=None):
        s = self.settings if provider is None else Settings({"brief": {"chat": {"provider": provider}}}, Path("s.json"))
        which = lambda name: ("/bin/" + name) if name in tools else None  # noqa: E731
        with mock.patch.object(briefchat.shutil, "which", side_effect=which), \
                mock.patch.dict(os.environ, env, clear=True):
            return briefchat.available(s), briefchat.pick_provider(s)

    def test_claude_cli_preferred(self):
        have, pick = self.run_with({"claude", "gemini"}, {"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(have, ["claude-cli", "gemini-cli", "anthropic"])
        self.assertEqual(pick, "claude-cli")

    def test_gemini_over_anthropic(self):
        self.assertEqual(self.run_with({"gemini"}, {"ANTHROPIC_API_KEY": "k"})[1], "gemini-cli")

    def test_anthropic_alone(self):
        self.assertEqual(self.run_with(set(), {"ANTHROPIC_API_KEY": "k"})[1], "anthropic")

    def test_explicit_provider_only_if_available(self):
        self.assertEqual(self.run_with({"claude", "gemini"}, {}, provider="gemini-cli")[1], "gemini-cli")
        self.assertIsNone(self.run_with({"claude"}, {}, provider="gemini-cli")[1])

    def test_none_available(self):
        have, pick = self.run_with(set(), {})
        self.assertEqual(have, [])
        self.assertIsNone(pick)


class TestRunCli(unittest.TestCase):
    SCRIPT = "import sys; t = sys.stdin.read(); print('got ' + t); print('second')"

    def test_streams_output_into_ask(self):
        a = briefchat.Ask("a1", "q", "claude-cli")
        emit = mock.Mock()
        briefchat._run_cli([sys.executable, "-c", self.SCRIPT], "hello", a, emit)
        self.assertIn("got hello", a.text)
        self.assertIn("second", a.text)
        self.assertTrue(emit.called)

    def test_nonzero_exit_without_output_raises(self):
        a = briefchat.Ask("a1", "q", "claude-cli")
        script = "import sys; sys.stdin.read(); sys.stderr.write('boom happened'); sys.exit(3)"
        with self.assertRaises(RuntimeError) as cm:
            briefchat._run_cli([sys.executable, "-c", script], "x", a, lambda: None)
        self.assertIn("boom happened", str(cm.exception))

    def test_claude_cli_child_env_drops_api_key(self):
        a = briefchat.Ask("a1", "q", "claude-cli")
        with mock.patch.object(briefchat, "_run_cli") as run, \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "KEEP_ME": "1"}):
            run.side_effect = lambda argv, prompt, ask_obj, emit, env=None, cwd=None: setattr(ask_obj, "text", "ok")
            briefchat.ask(Settings({}, Path("s.json")), a, "prompt", lambda d: None)
        env = run.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env.get("KEEP_ME"), "1")
        self.assertEqual(run.call_args.kwargs["cwd"], os.path.expanduser("~"))
        self.assertEqual(run.call_args.args[1], "prompt")


class TestAsk(unittest.TestCase):
    def setUp(self):
        self.settings = Settings({}, Path("s.json"))

    def test_unknown_provider(self):
        a = briefchat.Ask("a1", "q", "nope")
        deltas = []
        briefchat.ask(self.settings, a, "p", deltas.append)
        self.assertTrue(a.error)
        self.assertTrue(a.done)
        self.assertTrue(deltas[-1]["done"])

    def test_provider_exception(self):
        a = briefchat.Ask("a1", "q", "anthropic")
        deltas = []
        with mock.patch.object(briefchat, "_run_anthropic", side_effect=ValueError("kaput")):
            briefchat.ask(self.settings, a, "p", deltas.append)
        self.assertIn("kaput", a.error)
        self.assertTrue(a.done)
        self.assertTrue(deltas[-1]["done"])
        self.assertIn("kaput", deltas[-1]["error"])

    def test_empty_answer(self):
        a = briefchat.Ask("a1", "q", "anthropic")
        with mock.patch.object(briefchat, "_run_anthropic"):
            briefchat.ask(self.settings, a, "p", lambda d: None)
        self.assertIn("empty answer", a.error)
        self.assertTrue(a.done)


class FakeStream(object):
    def __init__(self, chunks, stop_reason):
        self.text_stream = iter(chunks)
        self._stop = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return types.SimpleNamespace(stop_reason=self._stop)


class TestRunAnthropic(unittest.TestCase):
    def run_it(self, chunks, stop="end_turn", settings=None):
        stream_fn = mock.Mock(return_value=FakeStream(chunks, stop))
        client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream_fn))
        fake = types.ModuleType("anthropic")
        fake.Anthropic = mock.Mock(return_value=client)
        a = briefchat.Ask("a1", "q", "anthropic")
        prompt = briefchat.build_prompt("why?", [], "CTX")
        with mock.patch.dict(sys.modules, {"anthropic": fake}):
            briefchat._run_anthropic(settings or Settings({}, Path("s.json")), prompt, a, lambda: None)
        return a, stream_fn

    def test_streams_text_with_defaults(self):
        a, stream_fn = self.run_it(["Hello ", "there\n", "- pt"])
        self.assertEqual(a.text, "Hello there\n- pt")
        kw = stream_fn.call_args.kwargs
        self.assertEqual(kw["model"], "claude-haiku-5-5")
        self.assertEqual(kw["output_config"], {"effort": "low"})
        self.assertIn(briefchat.SYSTEM, kw["system"])
        self.assertIn("Question: why?", kw["messages"][0]["content"])
        self.assertIn("CTX", kw["messages"][0]["content"])

    def test_settings_override_model(self):
        s = make_settings(tempfile.gettempdir(), model="claude-x", effort="medium")
        _, stream_fn = self.run_it(["a"], settings=s)
        self.assertEqual(stream_fn.call_args.kwargs["model"], "claude-x")
        self.assertEqual(stream_fn.call_args.kwargs["output_config"], {"effort": "medium"})

    def test_refusal_without_text_gets_a_fallback(self):
        a, _ = self.run_it([], stop="refusal")
        self.assertTrue(a.text.strip())

    def test_refusal_keeps_existing_text(self):
        a, _ = self.run_it(["partial"], stop="refusal")
        self.assertEqual(a.text, "partial")


class TestStateFields(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = make_settings(Path(self._tmp.name))

    def state(self, **over):
        s = brief.default_state("2026-11-14")
        s["v"] = 5
        s.update(over)
        return s

    def test_defaults(self):
        d = brief.default_state("x")
        self.assertEqual(d["ask"], "")
        self.assertIs(d["askShow"], False)

    def test_bad_ask_refused(self):
        for bad in ("A1", "a b", "x" * 41, "a;b", 5):
            with self.assertRaises(ValueError):
                brief.write_state(self.settings, self.state(ask=bad))

    def test_good_ask_and_bool_coercion(self):
        stored = brief.write_state(self.settings, self.state(ask="a1731600000000", askShow=1))
        self.assertEqual(stored["ask"], "a1731600000000")
        self.assertIs(stored["askShow"], True)


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestChatRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.work.mkdir()
        self.settings = make_settings(self.work)
        p = mock.patch.object(app_module, "settings", self.settings)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app_module.app, base_url="http://127.0.0.1")

    def write_brief(self):
        import json
        d = brief.brief_dir(self.settings)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.json").write_text(json.dumps(sample_brief()), encoding="utf-8")

    def test_status(self):
        with mock.patch.object(briefchat, "pick_provider", return_value="anthropic"), \
                mock.patch.object(briefchat, "available", return_value=["anthropic"]):
            body = self.client.get("/api/brief/chat/status").json()
        self.assertEqual(body["provider"], "anthropic")
        self.assertEqual(body["hint"], "")
        with mock.patch.object(briefchat, "pick_provider", return_value=None), \
                mock.patch.object(briefchat, "available", return_value=[]):
            body = self.client.get("/api/brief/chat/status").json()
        self.assertIsNone(body["provider"])
        self.assertIn("No chat model", body["hint"])

    def test_post_needs_header(self):
        self.assertEqual(self.client.post("/api/brief/chat", json={"question": "hi"}).status_code, 403)

    def test_empty_and_long_questions(self):
        for q in ("", "   "):
            self.assertEqual(self.client.post("/api/brief/chat", json={"question": q}, headers=HEADERS).status_code, 400)
        r = self.client.post("/api/brief/chat", json={"question": "x" * (briefchat.QUESTION_MAX + 1)}, headers=HEADERS)
        self.assertEqual(r.status_code, 400)

    def test_no_provider_is_503_with_hint(self):
        with mock.patch.object(briefchat, "pick_provider", return_value=None):
            r = self.client.post("/api/brief/chat", json={"question": "hi"}, headers=HEADERS)
        self.assertEqual(r.status_code, 503)
        self.assertIn("No chat model", r.json()["detail"])

    def ask_once(self, with_brief):
        if with_brief:
            self.write_brief()
        captured = {}

        def fake_ask(settings, ask_obj, prompt, on_delta):
            captured["prompt"] = prompt
            ask_obj.text = "yes\n- a"
            ask_obj.done = True
            on_delta(ask_obj.to_dict())
            return ask_obj

        class SyncThread(object):
            def __init__(self, target=None, args=(), daemon=None, **kw):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        fake_threading = types.SimpleNamespace(Thread=SyncThread)
        with mock.patch.object(briefchat, "pick_provider", return_value="anthropic"), \
                mock.patch.object(briefchat, "ask", side_effect=fake_ask), \
                mock.patch.object(app_module, "threading", fake_threading):
            r = self.client.post("/api/brief/chat", json={"question": "what is this?", "page": "dock"}, headers=HEADERS)
        return r, captured

    def test_success_without_a_brief(self):
        r, captured = self.ask_once(False)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["q"], "what is this?")
        self.assertEqual(body["provider"], "anthropic")
        self.assertTrue(body["id"])
        self.assertIn("what is this?", captured["prompt"])
        got = self.client.get("/api/brief/chat/" + body["id"])
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["head"], "yes")
        self.assertEqual(got.json()["points"], ["a"])
        self.assertTrue(got.json()["done"])

    def test_success_with_a_brief_puts_it_in_the_prompt(self):
        r, captured = self.ask_once(True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tonight's theme: k", captured["prompt"])

    def test_unknown_id_404(self):
        self.assertEqual(self.client.get("/api/brief/chat/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
