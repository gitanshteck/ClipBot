"""stages/events.py, the `events` commands, and what the reaction map does to moment ranking and analyze's annotations.
The model is never loaded: `panns.load_model` / `panns.tag_second_grid` are replaced by fakes that record what they were asked."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from clipbot import audioevents, editcli, editmoment, highlights, panns
from clipbot.config import Settings
from clipbot.stages import events as events_stage
from clipbot.utils import StageError
from clipbot.workspace import Workspace


def make_ws(tmp, seconds=60, slug="ws"):
    root = Path(tmp) / slug
    root.mkdir(parents=True)
    ws = Workspace.open(root).ensure()
    with wave.open(str(ws.audio_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(seconds * 16000, dtype="<i2").tobytes())
    return ws


def fake_grid(laugh_at=(), shout_at=(), gun_at=()):
    """A stand-in for panns.tag_second_grid: probabilities for the union classes, with chosen seconds lit."""
    classes = audioevents.union_classes()
    col = {c: i for i, c in enumerate(classes)}

    def grid(model, wav_path, duration=None, first=0, last=None, classes=None, progress=None, cancelled=None):
        n = int(last if last is not None else np.ceil(duration)) - first
        out = np.zeros((n, len(classes)), dtype=np.float32)
        for s in laugh_at:
            if 0 <= s - first < n:
                out[s - first, col[16]] = 0.8
        for s in shout_at:
            if 0 <= s - first < n:
                out[s - first, col[8]] = 0.9
        for s in gun_at:
            if 0 <= s - first < n:
                out[s - first, col[427]] = 0.7
        if progress:
            progress(n, n)
        return out

    return grid


class StageCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.ws = make_ws(self.tmp)
        self.settings = Settings({"work_root": self.tmp}, Path("s.json"))
        self.patches = [mock.patch.object(panns, "load_model", return_value=object()),
                        mock.patch.object(panns, "tag_second_grid", side_effect=fake_grid(laugh_at=(10, 11, 12), shout_at=(30,), gun_at=(45,)))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()


class TestScan(StageCase):
    def test_a_scan_writes_events_json_with_categories_peaks_and_marks_the_stage(self):
        path = events_stage.scan_events(self.ws, self.settings)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path, self.ws.root / "events.json")
        self.assertEqual(doc["seconds"], 60)
        self.assertEqual(doc["categories"]["laughter"][11], 80)
        self.assertEqual([p["t"] for p in doc["peaks"]["laughter"]], [10])         # 10, 11, 12 are one run; 10 is its first highest
        self.assertEqual(doc["peaks"]["shout"][0]["p"], 0.9)
        self.assertEqual(doc["peaks"]["gunfire"][0]["t"], 45)
        self.assertEqual(doc["model"]["md5"], panns.CHECKPOINT_MD5)
        self.assertTrue(self.ws.stage_done("events"))

    def test_a_scan_that_exists_is_not_redone_without_force(self):
        events_stage.scan_events(self.ws, self.settings)
        self.assertEqual(panns.load_model.call_count, 1)
        events_stage.scan_events(self.ws, self.settings)
        self.assertEqual(panns.load_model.call_count, 1)
        events_stage.scan_events(self.ws, self.settings, force=True)
        self.assertEqual(panns.load_model.call_count, 2)

    def test_a_partial_scan_records_where_it_starts_and_puts_peaks_on_the_streams_clock(self):
        path = events_stage.scan_events(self.ws, self.settings, first=20, last=50)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((doc["first"], doc["seconds"]), (20, 30))
        self.assertEqual(doc["peaks"]["shout"][0]["t"], 30)
        self.assertEqual(audioevents.score_at(doc, "shout", 30), 0.9)
        self.assertEqual(audioevents.score_at(doc, "laughter", 11), 0.0)           # before what was scanned

    def test_no_audio_is_an_error_that_says_what_to_run(self):
        self.ws.audio_path.unlink()
        with self.assertRaises(StageError) as ctx:
            events_stage.scan_events(self.ws, self.settings)
        self.assertIn("audio", str(ctx.exception))

    def test_a_missing_checkpoint_is_the_models_own_error_and_leaves_nothing_behind(self):
        panns.load_model.side_effect = StageError("the PANNs checkpoint is not at x; fetch it with: clipbot events fetch-model")
        with self.assertRaises(StageError):
            events_stage.scan_events(self.ws, self.settings)
        self.assertFalse((self.ws.root / "events.json").exists())

    def test_cancelling_stops_the_scan_without_a_file(self):
        from clipbot.progress import JobCancelled

        panns.tag_second_grid.side_effect = lambda *a, **k: (_ for _ in ()).throw(JobCancelled("cancelled"))
        with self.assertRaises(JobCancelled):
            events_stage.scan_events(self.ws, self.settings)
        self.assertFalse((self.ws.root / "events.json").exists())
        self.assertFalse(self.ws.stage_done("events"))


def run_cmd(func, settings, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = func(argparse.Namespace(**kw), settings)
    return code, out.getvalue()


class TestCommands(StageCase):
    def test_scan_prints_what_it_found(self):
        code, out = run_cmd(editcli.cmd_events_scan, self.settings, workspace=str(self.ws.root), force=False, first=None, last=None)
        self.assertEqual(code, 0)
        self.assertIn("60 s tagged", out)
        self.assertIn("laughter", out)

    def test_peaks_lists_the_strongest_with_the_transcript_nearby(self):
        events_stage.scan_events(self.ws, self.settings)
        self.ws.write_json(self.ws.transcript_path, {"segments": [{"id": 0, "start": 9.0, "end": 12.0, "text": "ha ha ha yaar", "low_confidence": False}]})
        code, out = run_cmd(editcli.cmd_events_peaks, self.settings, workspace=str(self.ws.root), category="laughter", top=5, min_p=0.0, min_gap=20.0)
        self.assertEqual(code, 0)
        self.assertIn("p=0.80", out)
        self.assertIn("ha ha ha yaar", out)

    def test_peaks_without_a_scan_says_what_to_run_and_an_unknown_category_lists_the_real_ones(self):
        with self.assertRaises(StageError) as ctx:
            run_cmd(editcli.cmd_events_peaks, self.settings, workspace=str(self.ws.root), category="laughter", top=5, min_p=0.0, min_gap=20.0)
        self.assertIn("events scan", str(ctx.exception))
        events_stage.scan_events(self.ws, self.settings)
        with self.assertRaises(StageError) as ctx:
            run_cmd(editcli.cmd_events_peaks, self.settings, workspace=str(self.ws.root), category="applause", top=5, min_p=0.0, min_gap=20.0)
        self.assertIn("laughter", str(ctx.exception))

    def test_fetch_model_reports_a_file_that_is_already_there_and_downloads_nothing(self):
        with mock.patch.object(panns, "verify_checkpoint", return_value=True), \
                mock.patch.object(panns, "fetch_checkpoint", side_effect=AssertionError("downloaded")):
            code, out = run_cmd(editcli.cmd_events_fetch_model, self.settings)
        self.assertEqual(code, 0)
        self.assertIn("already here", out)

    def test_check_measures_gunfire_against_the_kill_record_and_laughter_against_chat(self):
        events_stage.scan_events(self.ws, self.settings)
        self.ws.write_json(self.ws.root / "killfeed.json", {"events": [{"id": "k1", "t": 45.0}, {"id": "k2", "t": 46.0}]})
        msgs = [{"offset": 20.0 + 3.0 * i, "text": "KEKW"} for i in range(6)]
        self.ws.write_json(self.ws.chat_path, {"status": "ok", "messages": msgs})
        code, out = run_cmd(editcli.cmd_events_check, self.settings, workspace=str(self.ws.root), top=3)
        self.assertEqual(code, 0)
        self.assertIn("against 2 kills", out)
        self.assertIn("gunfire", out)
        self.assertIn("against 6 chat messages that laugh", out)
        self.assertIn("loudness", out)
        self.assertIn("strongest laughter peaks", out)

    def test_check_with_nothing_to_check_against_says_so_and_without_a_scan_says_what_to_run(self):
        with self.assertRaises(StageError):
            run_cmd(editcli.cmd_events_check, self.settings, workspace=str(self.ws.root), top=3)
        events_stage.scan_events(self.ws, self.settings)
        code, out = run_cmd(editcli.cmd_events_check, self.settings, workspace=str(self.ws.root), top=3)
        self.assertIn("nothing to check against", out)

    def test_the_parsers_register_the_commands(self):
        from clipbot import cli

        for argv, name in ((["events", "fetch-model"], "cmd_events_fetch_model"), (["events", "scan", "--workspace", "w"], "cmd_events_scan"),
                           (["events", "peaks", "--workspace", "w"], "cmd_events_peaks"), (["events", "tags", "--workspace", "w", "--at", "5"], "cmd_events_tags")):
            self.assertIs(cli.build_parser().parse_args(argv).func, getattr(editcli, name))


class TestMomentScoring(unittest.TestCase):
    ARC = {"id": "m001", "start": 100.0, "end": 140.0, "first": 110.0, "last": 120.0,
           "kills": [{"t": 110.0, "weapon": "gun", "dist_m": 20.0, "headshot": False, "source": "feed"}]}

    def events_doc(self, laugh_at=None):
        series = {name: np.zeros(200, dtype=np.float32) for name in panns.CATEGORIES}
        if laugh_at is not None:
            series["laughter"][laugh_at] = 0.8
        series["gunfire"][112] = 0.99
        return audioevents.build_document("ws", 200.0, series)

    def test_the_reaction_map_does_not_touch_a_ranking_unless_it_is_given_a_weight(self):
        base = editmoment.score_arc(dict(self.ARC))
        with_map = editmoment.score_arc(dict(self.ARC), events=self.events_doc(laugh_at=125))
        self.assertEqual(editmoment.W_VOICE, 0.0)                      # off until it is shown to find funny moments
        self.assertEqual(with_map, base)
        self.assertNotIn("voice", with_map["parts"])

    def test_without_a_reaction_map_there_is_no_voice_part_not_a_zero_one(self):
        self.assertNotIn("voice", editmoment.score_arc(dict(self.ARC), voice_weight=2.0)["parts"])

    def test_a_laugh_inside_the_arc_adds_a_voice_part_and_raises_the_score(self):
        base = editmoment.score_arc(dict(self.ARC))["score"]
        r = editmoment.score_arc(dict(self.ARC), events=self.events_doc(laugh_at=125), voice_weight=2.0)
        self.assertEqual(r["parts"]["voice"], 1.6)
        self.assertAlmostEqual(r["score"], base + 1.6, places=2)

    def test_a_laugh_outside_the_arc_and_gunfire_inside_it_add_nothing(self):
        r = editmoment.score_arc(dict(self.ARC), events=self.events_doc(laugh_at=50), voice_weight=2.0)
        self.assertEqual(r["parts"]["voice"], 0.0)                      # gunfire at 112 is not a reaction

    def build_ws(self, tmp, **moments):
        ws = make_ws(tmp, seconds=200)
        ws.write_json(ws.root / "killfeed.json", {"duration": 200.0, "events": [
            {"id": "k001", "t": 110.0, "first": 109.0, "last": 111.0, "weapon": "gun", "dist_m": 30.0, "headshot": False,
             "through_cover": False, "source": "feed", "icon": None}]})
        ws.update_state(duration=200.0)
        return ws, Settings({"work_root": tmp, "moments": moments}, Path("s.json"))

    def test_build_records_that_a_reaction_map_exists_and_scores_with_it_only_when_weighted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, settings = self.build_ws(tmp)
            without = editmoment.build(ws, settings, snap=False)
            self.assertFalse(without["signals"]["events"])
            ws.write_json(ws.root / "events.json", self.events_doc(laugh_at=112))
            unweighted = editmoment.build(ws, settings, snap=False)
            self.assertTrue(unweighted["signals"]["events"])
            self.assertNotIn("voice", unweighted["arcs"][0]["parts"])
            self.assertEqual(unweighted["arcs"][0]["score"], without["arcs"][0]["score"])
            weighted = Settings({"work_root": tmp, "moments": {"voice_weight": 2.0}}, Path("s.json"))
            on = editmoment.build(ws, weighted, snap=False)
            self.assertEqual(on["arcs"][0]["parts"]["voice"], 1.6)


class TestAnalyzeAnnotations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self._tmp.name)
        series = {name: np.zeros(60, dtype=np.float32) for name in panns.CATEGORIES}
        series["laughter"][10:13] = [0.3, 0.8, 0.5]
        series["shout"][30] = 0.45
        series["shout"][40] = 0.3
        series["gunfire"][50] = 0.95
        self.ws.write_json(self.ws.root / "events.json", audioevents.build_document("ws", 60.0, series))

    def tearDown(self):
        self._tmp.cleanup()

    def settings(self, **signals):
        return Settings({"work_root": self._tmp.name, "analyze": {"signals": signals}}, Path("s.json"))

    def test_it_is_off_by_default_so_a_scanned_stream_is_annotated_exactly_as_before(self):
        self.assertEqual(highlights.event_moments(self.ws, self.settings()), [])
        with mock.patch.object(highlights, "compute_energy_spikes", return_value=[]):
            self.assertEqual(highlights.notable_moments(self.ws, self.settings()), [])

    def test_laughter_and_shouting_become_labelled_moments_and_gunfire_does_not(self):
        labels = [m["label"] for m in highlights.event_moments(self.ws, self.settings(events=True))]
        self.assertEqual(labels, ["laughter, 0.8", "shouting, 0.5"])          # 0.3 shout is under events_min_p (0.4)

    def test_the_floor_is_a_setting(self):
        self.assertEqual(len(highlights.event_moments(self.ws, self.settings(events=True, events_min_p=0.25))), 3)

    def test_no_events_json_is_no_moments_and_notable_moments_carries_them_when_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = make_ws(tmp)
            s = Settings({"work_root": tmp, "analyze": {"signals": {"events": True}}}, Path("s.json"))
            self.assertEqual(highlights.event_moments(bare, s), [])
        with mock.patch.object(highlights, "compute_energy_spikes", return_value=[]):
            labels = [m["label"] for m in highlights.notable_moments(self.ws, self.settings(events=True))]
        self.assertIn("laughter, 0.8", labels)


if __name__ == "__main__":
    unittest.main()
