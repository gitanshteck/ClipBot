"""Speaker diarization/assignment: clipbot/speakers.py's pure merge logic and
workspace-backed assignment, reelspec.py's `speakers` block + slot layout,
and the same legacy-path-unaffected invariant already applied to fx/captions
(a speakers-disabled clip's filter graph/argv must be unaffected by the
feature existing at all).

Run: python -m unittest discover -s tests
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipbot import reelspec as R
from clipbot import speakers as sp
from clipbot.specerror import SpecError
from clipbot.utils import StageError
from clipbot.workspace import Workspace


def _spec(speakers=None):
    spec = {"preset": "cam_top", "canvas": "1080x1920",
            "src": {"cam": dict(R.DEFAULT_CAM)}}
    if speakers is not None:
        spec["speakers"] = speakers
    return spec


class TestResolvedSegmentsAndSpans(unittest.TestCase):
    """Pure functions - no workspace needed."""

    def test_resolved_segments_merges_assignments(self):
        transcript = {"segments": [
            {"id": 0, "start": 0.0, "end": 2.0, "text": "a"},
            {"id": 1, "start": 2.0, "end": 4.0, "text": "b"},
        ]}
        map_doc = {"assignments": {"0": "host"}}
        out = sp.resolved_segments(transcript, map_doc)
        self.assertEqual(out[0]["speaker_id"], "host")
        self.assertIsNone(out[1]["speaker_id"])

    def test_speaking_spans_merges_adjacent_same_speaker(self):
        segments = [
            {"id": 0, "start": 0.0, "end": 2.0, "speaker_id": "host"},
            {"id": 1, "start": 2.0, "end": 4.0, "speaker_id": "host"},
            {"id": 2, "start": 10.0, "end": 12.0, "speaker_id": "guest"},
            {"id": 3, "start": 12.0, "end": 13.0, "speaker_id": None},
        ]
        spans = sp.speaking_spans(segments)
        self.assertEqual(spans, [
            {"speaker_id": "host", "start": 0.0, "end": 4.0},
            {"speaker_id": "guest", "start": 10.0, "end": 12.0},
        ])

    def test_speaking_spans_splits_on_a_long_gap(self):
        segments = [
            {"id": 0, "start": 0.0, "end": 2.0, "speaker_id": "host"},
            {"id": 1, "start": 10.0, "end": 12.0, "speaker_id": "host"},
        ]
        spans = sp.speaking_spans(segments, max_gap=2.0)
        self.assertEqual(len(spans), 2)


class TestWorkspaceAssignment(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws").ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def test_assign_range_and_clear(self):
        sp.assign_range(self.ws, 0, 2, "host")
        doc = sp.load(self.ws)
        self.assertEqual(doc["assignments"], {"0": "host", "1": "host", "2": "host"})

        sp.assign_range(self.ws, 1, 1, None)
        doc = sp.load(self.ws)
        self.assertEqual(doc["assignments"], {"0": "host", "2": "host"})

    def test_rename_speaker_bulk_rewrites(self):
        sp.assign_range(self.ws, 0, 3, "SPEAKER_00")
        sp.rename_speaker(self.ws, "SPEAKER_00", "host")
        doc = sp.load(self.ws)
        self.assertTrue(all(v == "host" for v in doc["assignments"].values()))

    def test_bulk_from_diarization_picks_best_overlap(self):
        transcript = {"segments": [
            {"id": 0, "start": 0.0, "end": 2.0, "text": "a"},
            {"id": 1, "start": 2.0, "end": 4.0, "text": "b"},
        ]}
        diarization = {"turns": [
            {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
        ]}
        sp.bulk_from_diarization(self.ws, transcript, diarization)
        doc = sp.load(self.ws)
        self.assertEqual(doc["assignments"], {"0": "SPEAKER_00", "1": "SPEAKER_00"})

    def test_bulk_from_diarization_never_overwrites_by_default(self):
        sp.assign_range(self.ws, 0, 0, "host")  # a human already named this one
        transcript = {"segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": "a"}]}
        diarization = {"turns": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}]}
        sp.bulk_from_diarization(self.ws, transcript, diarization)
        doc = sp.load(self.ws)
        self.assertEqual(doc["assignments"]["0"], "host")  # untouched

        sp.bulk_from_diarization(self.ws, transcript, diarization, overwrite=True)
        doc = sp.load(self.ws)
        self.assertEqual(doc["assignments"]["0"], "SPEAKER_00")  # explicit overwrite wins


class TestSpeakersBlockValidation(unittest.TestCase):
    def test_disabled_or_missing_is_omitted(self):
        self.assertNotIn("speakers", R.normalize(_spec()))
        self.assertNotIn("speakers", R.normalize(_spec({"enabled": False})))

    def test_defaults_fill_in(self):
        out = R.normalize(_spec({}))["speakers"]
        self.assertEqual(out["mode"], "appear")
        self.assertEqual(out["edge"], "bottom")
        self.assertIsNone(out["roster"])

    def test_bad_mode_rejected(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"mode": "karaoke"}))

    def test_avatar_size_out_of_range_rejected(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"avatar_size": 0.9}))

    def test_roster_too_long_rejected(self):
        with self.assertRaises(SpecError):
            R.normalize(_spec({"roster": ["s{0}".format(i) for i in range(9)]}))

    def test_roster_accepts_ids(self):
        out = R.normalize(_spec({"roster": ["host", "guest"]}))["speakers"]
        self.assertEqual(out["roster"], ["host", "guest"])


class TestLayoutSpeakerSlots(unittest.TestCase):
    def test_no_speakers_plan_or_no_active_ids(self):
        self.assertEqual(R.layout_speaker_slots(None, (1080, 1920), ["host"]), [])
        plan = R.normalize(_spec({}))["speakers"]
        self.assertEqual(R.layout_speaker_slots(plan, (1080, 1920), []), [])

    def test_single_speaker_is_centered(self):
        plan = R.normalize(_spec({"avatar_size": 0.2}))["speakers"]
        slots = R.layout_speaker_slots(plan, (1080, 1920), ["host"])
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertAlmostEqual(slot["x"] + slot["w"] / 2, 540, delta=2)  # centered on 1080 width

    def test_roster_wins_over_active_ids(self):
        plan = R.normalize(_spec({"roster": ["a", "b", "c"]}))["speakers"]
        slots = R.layout_speaker_slots(plan, (1080, 1920), ["z"])  # active id ignored
        self.assertEqual([s["speaker_id"] for s in slots], ["a", "b", "c"])

    def test_top_edge_near_zero_bottom_edge_near_canvas_height(self):
        plan_top = R.normalize(_spec({"edge": "top"}))["speakers"]
        plan_bottom = R.normalize(_spec({"edge": "bottom"}))["speakers"]
        top_slot = R.layout_speaker_slots(plan_top, (1080, 1920), ["host"])[0]
        bottom_slot = R.layout_speaker_slots(plan_bottom, (1080, 1920), ["host"])[0]
        self.assertLess(top_slot["y"], 200)
        self.assertGreater(bottom_slot["y"], 1920 - 200 - bottom_slot["h"])


class TestSpeakersLegacyPathUnaffected(unittest.TestCase):
    def test_build_filter_ignores_none_speaker_plan(self):
        plan = R.resolve(_spec(), 1280, 720)
        self.assertEqual(R.build_filter(plan), R.build_filter(plan, speaker_plan=None))

    def test_build_argv_ignores_none_speaker_plan(self):
        plan = R.resolve(_spec(), 1280, 720)
        base = R.build_argv("ffmpeg", "SRC.mp4", "OUT.mp4", 10.0, 45.0, plan)
        self.assertEqual(
            base,
            R.build_argv("ffmpeg", "SRC.mp4", "OUT.mp4", 10.0, 45.0, plan, speaker_plan=None),
        )

    def test_speaker_plan_forces_the_fx_aware_path(self):
        plan = R.resolve(_spec({}), 1280, 720)
        fake_plan = {"video": [{"index": 5, "w": 100, "h": 100, "x": 0, "y": 0,
                                "start": 0.0, "end": 5.0, "fade": 0.0}], "inputs": []}
        graph = R.build_filter(plan, speaker_plan=fake_plan)
        self.assertIn("[5:v]scale=100:100", graph)
        self.assertIn("format=yuv420p[v]", graph)


class TestResolveSpeakers(unittest.TestCase):
    """speakerfx.resolve_speakers needs a real (tiny) source image on disk
    for the circular-crop step, and a real cache directory to write into."""

    def setUp(self):
        from PIL import Image

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.cache_dir = self.tmp_path / "cache"
        self.avatar_src = self.tmp_path / "host.png"
        Image.new("RGBA", (64, 64), (10, 20, 30, 255)).save(self.avatar_src)

        self.speakers_plan = {
            "enabled": True, "mode": "appear", "edge": "bottom",
            "avatar_size": 0.16, "gap": 0.02, "ring_width_px": 6, "roster": None,
        }
        self.assets = {"avt_deadbeefdeadbeef": {"path": str(self.avatar_src)}}
        self.registry = {"host": {"id": "host", "name": "Host",
                                  "avatar_asset": "avt_deadbeefdeadbeef", "color": "#19A2D2"}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_appear_mode_one_entry_per_span(self):
        from clipbot import speakerfx

        spans = [{"speaker_id": "host", "start": 100.0, "end": 105.0}]
        plan = speakerfx.resolve_speakers(
            self.speakers_plan, spans, canvas=(1080, 1920), origin=95.0, span_duration=20.0,
            assets=self.assets, registry=self.registry, cache_dir=self.cache_dir,
            next_input=3, strict=True,
        )
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["video"]), 1)
        entry = plan["video"][0]
        self.assertEqual(entry["type"], "avatar")
        self.assertEqual(entry["index"], 3)
        self.assertAlmostEqual(entry["start"], 5.0)
        self.assertAlmostEqual(entry["end"], 10.0)
        self.assertTrue(Path(plan["inputs"][0]["args"][-1]).exists())

    def test_discord_mode_adds_always_on_avatar_plus_ring(self):
        from clipbot import speakerfx

        discord_plan = dict(self.speakers_plan, mode="discord")
        spans = [{"speaker_id": "host", "start": 100.0, "end": 105.0}]
        plan = speakerfx.resolve_speakers(
            discord_plan, spans, canvas=(1080, 1920), origin=95.0, span_duration=20.0,
            assets=self.assets, registry=self.registry, cache_dir=self.cache_dir,
            next_input=1, strict=True,
        )
        types = sorted(e["type"] for e in plan["video"])
        self.assertEqual(types, ["avatar", "ring"])
        avatar = next(e for e in plan["video"] if e["type"] == "avatar")
        self.assertEqual((avatar["start"], avatar["end"]), (0.0, 20.0))  # always on
        ring = next(e for e in plan["video"] if e["type"] == "ring")
        self.assertAlmostEqual(ring["start"], 5.0)

    def test_span_outside_window_is_dropped(self):
        from clipbot import speakerfx

        spans = [{"speaker_id": "host", "start": 500.0, "end": 505.0}]
        plan = speakerfx.resolve_speakers(
            self.speakers_plan, spans, canvas=(1080, 1920), origin=0.0, span_duration=20.0,
            assets=self.assets, registry=self.registry, cache_dir=self.cache_dir,
        )
        self.assertIsNone(plan)

    def test_missing_avatar_is_fatal_when_strict_and_a_warning_otherwise(self):
        from clipbot import speakerfx
        from clipbot.specerror import SpecError as SE

        spans = [{"speaker_id": "nobody", "start": 1.0, "end": 2.0}]
        with self.assertRaises(SE):
            speakerfx.resolve_speakers(
                self.speakers_plan, spans, canvas=(1080, 1920), origin=0.0, span_duration=10.0,
                assets=self.assets, registry={}, cache_dir=self.cache_dir, strict=True,
            )
        plan = speakerfx.resolve_speakers(
            self.speakers_plan, spans, canvas=(1080, 1920), origin=0.0, span_duration=10.0,
            assets=self.assets, registry={}, cache_dir=self.cache_dir, strict=False,
        )
        self.assertIsNone(plan)  # nothing survived, but no exception


class TestLoadPipelineNoneReturn(unittest.TestCase):
    """Regression test: pyannote.audio's Pipeline.from_pretrained does not
    raise on a bad/gated token - it logs a hint and returns None. Caught live
    against a real workspace: unchecked, that surfaced three lines later as
    'TypeError: NoneType object is not callable', which points nowhere near
    the actual cause. _load_pipeline must turn a None return into a clean
    StageError naming the real problem."""

    def test_none_pipeline_raises_a_clear_stage_error(self):
        from clipbot.config import Settings
        from clipbot.stages import diarize as diarize_stage

        settings = Settings({}, Path("config/settings.json"))
        with mock.patch.dict(os.environ, {"HF_TOKEN": "fake-token-for-this-test"}):
            with mock.patch("pyannote.audio.Pipeline.from_pretrained", return_value=None):
                with self.assertRaises(StageError) as ctx:
                    diarize_stage._load_pipeline(settings)
        self.assertIn("Could not load", str(ctx.exception))

    def test_gated_submodel_attribute_error_raises_a_clear_stage_error(self):
        """Caught live against a real workspace: a gated *sub*-model
        (pyannote/segmentation-3.0) whose agreement isn't accepted surfaces
        deep inside pyannote's own pipeline construction as
        AttributeError("'NoneType' object has no attribute 'eval'") -
        get_model() calls Model.from_pretrained() (which returns None on a
        gated model, same pattern as the top-level pipeline) and then
        unconditionally calls .eval() on the result with no None-check."""
        from clipbot.config import Settings
        from clipbot.stages import diarize as diarize_stage

        settings = Settings({}, Path("config/settings.json"))
        with mock.patch.dict(os.environ, {"HF_TOKEN": "fake-token-for-this-test"}):
            with mock.patch(
                "pyannote.audio.Pipeline.from_pretrained",
                side_effect=AttributeError("'NoneType' object has no attribute 'eval'"),
            ):
                with self.assertRaises(StageError) as ctx:
                    diarize_stage._load_pipeline(settings)
        self.assertIn("sub-model", str(ctx.exception))
        self.assertIn("segmentation-3.0", str(ctx.exception))

    def test_trust_pyannote_checkpoints_forces_weights_only_false(self):
        """Regression test: caught live against a real workspace. PyTorch
        2.6 defaults torch.load's weights_only to True, and pyannote's own
        checkpoints store plain objects (torch.torch_version.TorchVersion)
        alongside tensors, so loading one raises UnpicklingError under the
        new default. _trust_pyannote_checkpoints must make torch.load
        succeed on such a file regardless of what weights_only value the
        caller itself passes (pyannote's pl_load always explicitly passes
        one, so a functools.partial default alone would not survive being
        overridden back by that explicit keyword)."""
        import torch

        from clipbot.stages import diarize as diarize_stage

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake_checkpoint.pt"
            checkpoint = {
                "state_dict": {"w": torch.zeros(2)},
                "pytorch-lightning_version": torch.torch_version.TorchVersion("2.8.0"),
            }
            torch.save(checkpoint, path)

            # Establish the failure this is a regression test for.
            with self.assertRaises(Exception):
                torch.load(path, map_location="cpu", weights_only=None)

            with diarize_stage._trust_pyannote_checkpoints():
                # weights_only=None mirrors pyannote's pl_load always
                # re-passing its own (defaulted) value explicitly.
                loaded = torch.load(path, map_location="cpu", weights_only=None)
            self.assertIn("pytorch-lightning_version", loaded)

            # The patch must not leak past the context manager.
            with self.assertRaises(Exception):
                torch.load(path, map_location="cpu", weights_only=None)


if __name__ == "__main__":
    unittest.main()
