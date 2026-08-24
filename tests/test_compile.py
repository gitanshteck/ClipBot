"""Tests for clipbot/compilations.py and clipbot/stages/compile.py.

The ffmpeg-invoking parts of `render_compilation` are exercised with
`_run_ffmpeg`/`resolve_tool` stubbed out (same reasoning cut.py's own
untested subprocess calls already rely on manual/integration verification
for) - what's tested here is the control flow around them: fingerprinting,
skip-if-unchanged, segment caching, the ffconcat list shape, and the error
paths a bad compilation definition should hit.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipbot import compilations as comp
from clipbot.config import Settings
from clipbot.stages import compile as compile_stage
from clipbot.utils import StageError
from clipbot.workspace import Workspace


def _settings(overrides=None):
    data = {
        "tools": {"ffmpeg": "ffmpeg-stub", "ffprobe": "ffprobe-stub"},
        "cut": {"pad_start": 1.0, "pad_end": 1.5},
        "compile": {"min_duration": 0.5, "encoder": "libx264", "preset": "slow", "crf": 18},
    }
    if overrides:
        for dotted, value in overrides.items():
            node = data
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
    return Settings(data, Path("settings.json"))


class TestCompilationsSidecar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws").ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def test_upsert_creates_and_sorts_segments(self):
        created = comp.upsert(
            self.ws,
            "catan-highlights",
            [
                {"start": 200.0, "end": 210.0, "label": "b"},
                {"start": 100.0, "end": 110.0, "label": "a"},
            ],
            _settings(),
        )
        self.assertEqual(created["id"], "comp_0001")
        self.assertEqual([s["start"] for s in created["segments"]], [100.0, 200.0])
        self.assertEqual(created["segments"][0]["label"], "a")
        self.assertIsNone(created["output"])

    def test_upsert_replaces_existing_by_name_keeps_id(self):
        first = comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], _settings())
        second = comp.upsert(
            self.ws, "x", [{"start": 10.0, "end": 15.0, "label": "new"}], _settings()
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(second["segments"]), 1)
        self.assertEqual(second["segments"][0]["label"], "new")

    def test_upsert_rejects_backwards_range(self):
        with self.assertRaises(ValueError):
            comp.upsert(self.ws, "x", [{"start": 10.0, "end": 5.0}], _settings())

    def test_upsert_rejects_empty_segments(self):
        with self.assertRaises(ValueError):
            comp.upsert(self.ws, "x", [], _settings())

    def test_upsert_rejects_overlapping_segments(self):
        with self.assertRaises(ValueError):
            comp.upsert(
                self.ws,
                "x",
                [{"start": 0.0, "end": 10.0}, {"start": 5.0, "end": 15.0}],
                _settings(),
            )

    def test_upsert_rejects_segment_shorter_than_min_duration(self):
        with self.assertRaises(ValueError):
            comp.upsert(
                self.ws,
                "x",
                [{"start": 0.0, "end": 0.2}],
                _settings({"compile.min_duration": 0.5}),
            )

    def test_upsert_rejects_unsafe_name(self):
        for bad in ("../evil", "a/b", "a\\b", "..", ""):
            with self.assertRaises(ValueError):
                comp.upsert(self.ws, bad, [{"start": 0.0, "end": 5.0}], _settings())

    def test_set_output_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            comp.set_output(self.ws, "nope", {"file": "x"})

    def test_get_returns_none_for_missing_name(self):
        doc = comp.load(self.ws)
        self.assertIsNone(comp.get(doc, "nope"))

    def test_add_segment_creates_when_missing(self):
        created = comp.add_segment(
            self.ws, "new-one", {"start": 5.0, "end": 10.0, "label": "a"}, _settings()
        )
        self.assertEqual(created["name"], "new-one")
        self.assertEqual(len(created["segments"]), 1)
        self.assertEqual(created["segments"][0]["label"], "a")

    def test_add_segment_appends_and_resorts_existing(self):
        comp.upsert(
            self.ws, "x", [{"start": 100.0, "end": 110.0, "label": "second"}], _settings()
        )
        updated = comp.add_segment(
            self.ws, "x", {"start": 10.0, "end": 20.0, "label": "first"}, _settings()
        )
        self.assertEqual(len(updated["segments"]), 2)
        self.assertEqual([s["label"] for s in updated["segments"]], ["first", "second"])

    def test_add_segment_rejects_backwards_range(self):
        with self.assertRaises(ValueError):
            comp.add_segment(self.ws, "x", {"start": 10.0, "end": 5.0}, _settings())

    def test_add_segment_rejects_overlap_with_existing(self):
        comp.upsert(self.ws, "x", [{"start": 100.0, "end": 110.0}], _settings())
        with self.assertRaises(ValueError):
            comp.add_segment(self.ws, "x", {"start": 105.0, "end": 115.0}, _settings())

    def test_add_segment_rejects_unsafe_name(self):
        with self.assertRaises(ValueError):
            comp.add_segment(self.ws, "../evil", {"start": 0.0, "end": 5.0}, _settings())

    def test_delete_removes_entry(self):
        comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], _settings())
        comp.delete(self.ws, "x")
        doc = comp.load(self.ws)
        self.assertIsNone(comp.get(doc, "x"))

    def test_delete_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            comp.delete(self.ws, "nope")

    def test_delete_rejects_unsafe_name(self):
        with self.assertRaises(ValueError):
            comp.delete(self.ws, "../evil")

    def test_delete_purges_rendered_file_and_scratch_dir(self):
        comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], _settings())
        self.ws.compilations_dir.mkdir(parents=True, exist_ok=True)
        mp4 = self.ws.compilations_dir / "x.mp4"
        mp4.write_bytes(b"fake")
        scratch = self.ws.compile_scratch_dir("x")
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "001-abc.mp4").write_bytes(b"fake-segment")

        comp.delete(self.ws, "x")

        self.assertFalse(mp4.exists())
        self.assertFalse(scratch.exists())

    def test_delete_without_purge_leaves_files(self):
        comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], _settings())
        self.ws.compilations_dir.mkdir(parents=True, exist_ok=True)
        mp4 = self.ws.compilations_dir / "x.mp4"
        mp4.write_bytes(b"fake")

        comp.delete(self.ws, "x", purge=False)

        self.assertTrue(mp4.exists())

    def test_delete_purge_is_best_effort_when_files_never_existed(self):
        comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], _settings())
        # No rendered file, no scratch dir - must not raise.
        comp.delete(self.ws, "x")
        doc = comp.load(self.ws)
        self.assertIsNone(comp.get(doc, "x"))


class TestCrossStreamSegments(unittest.TestCase):
    """Segments carrying a `slug` different from the home workspace's own -
    the ordering/overlap-check branch in `_normalize_segments`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "home").ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def test_segment_without_slug_defaults_to_home_workspace(self):
        created = comp.upsert(
            self.ws, "x", [{"start": 0.0, "end": 5.0}], _settings()
        )
        self.assertEqual(created["segments"][0]["slug"], "home")

    def test_single_source_still_sorts_by_start(self):
        # All segments share one (explicit) foreign slug - still a single
        # source, so today's chronological-sort behavior applies unchanged.
        created = comp.upsert(
            self.ws,
            "x",
            [
                {"start": 200.0, "end": 210.0, "label": "b", "slug": "other"},
                {"start": 100.0, "end": 110.0, "label": "a", "slug": "other"},
            ],
            _settings(),
        )
        self.assertEqual([s["label"] for s in created["segments"]], ["a", "b"])

    def test_multi_source_preserves_given_order(self):
        # A cross-stream montage is editorially sequenced, not chronological -
        # the numerically-later segment is deliberately listed first.
        created = comp.upsert(
            self.ws,
            "x",
            [
                {"start": 200.0, "end": 210.0, "label": "first", "slug": "other"},
                {"start": 10.0, "end": 20.0, "label": "second"},
            ],
            _settings(),
        )
        self.assertEqual(
            [s["label"] for s in created["segments"]], ["first", "second"]
        )

    def test_multi_source_allows_identical_ranges_from_different_sources(self):
        # Same numeric range, different workspaces - not a real overlap.
        created = comp.upsert(
            self.ws,
            "x",
            [
                {"start": 100.0, "end": 110.0, "label": "a", "slug": "stream-a"},
                {"start": 100.0, "end": 110.0, "label": "b", "slug": "stream-b"},
            ],
            _settings(),
        )
        self.assertEqual(len(created["segments"]), 2)

    def test_multi_source_still_rejects_overlap_within_one_source(self):
        with self.assertRaises(ValueError):
            comp.upsert(
                self.ws,
                "x",
                [
                    {"start": 0.0, "end": 10.0, "slug": "other"},
                    {"start": 5.0, "end": 15.0, "slug": "other"},
                    {"start": 50.0, "end": 60.0},  # unrelated home-workspace segment
                ],
                _settings(),
            )


class TestFingerprints(unittest.TestCase):
    def test_segment_fingerprint_stable_and_sensitive(self):
        s = _settings()
        fp1 = compile_stage._segment_fingerprint(10.0, 20.0, "ws", s)
        fp2 = compile_stage._segment_fingerprint(10.0, 20.0, "ws", s)
        fp3 = compile_stage._segment_fingerprint(10.0, 21.0, "ws", s)
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)

    def test_segment_fingerprint_changes_with_encode_settings(self):
        fp_a = compile_stage._segment_fingerprint(10.0, 20.0, "ws", _settings())
        fp_b = compile_stage._segment_fingerprint(
            10.0, 20.0, "ws", _settings({"compile.crf": 22})
        )
        self.assertNotEqual(fp_a, fp_b)

    def test_segment_fingerprint_changes_with_source_slug(self):
        # Same numeric range, different source workspace - must not collide
        # in the scratch cache.
        fp_a = compile_stage._segment_fingerprint(10.0, 20.0, "stream-a", _settings())
        fp_b = compile_stage._segment_fingerprint(10.0, 20.0, "stream-b", _settings())
        self.assertNotEqual(fp_a, fp_b)

    def test_compilation_fingerprint_changes_with_ranges(self):
        s = _settings()
        fp_a = compile_stage._compilation_fingerprint("name", [("ws", 0.0, 5.0)], s)
        fp_b = compile_stage._compilation_fingerprint("name", [("ws", 0.0, 6.0)], s)
        fp_c = compile_stage._compilation_fingerprint("other", [("ws", 0.0, 5.0)], s)
        self.assertNotEqual(fp_a, fp_b)
        self.assertNotEqual(fp_a, fp_c)

    def test_compilation_fingerprint_changes_with_padding(self):
        fp_a = compile_stage._compilation_fingerprint(
            "name", [("ws", 0.0, 5.0)], _settings()
        )
        fp_b = compile_stage._compilation_fingerprint(
            "name", [("ws", 0.0, 5.0)], _settings({"cut.pad_start": 2.0})
        )
        self.assertNotEqual(fp_a, fp_b)

    def test_compilation_fingerprint_changes_with_source_slug(self):
        s = _settings()
        fp_a = compile_stage._compilation_fingerprint("name", [("stream-a", 0.0, 5.0)], s)
        fp_b = compile_stage._compilation_fingerprint("name", [("stream-b", 0.0, 5.0)], s)
        self.assertNotEqual(fp_a, fp_b)


class TestSegmentArgv(unittest.TestCase):
    def test_always_re_encodes_never_stream_copies(self):
        argv = compile_stage._segment_argv(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"), 10.0, 20.0, _settings()
        )
        self.assertIn("-c:v", argv)
        self.assertIn("libx264", argv)
        self.assertNotIn("copy", argv)

    def test_seek_before_input(self):
        argv = compile_stage._segment_argv(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"), 10.0, 20.0, _settings()
        )
        self.assertLess(argv.index("-ss"), argv.index("-i"))

    def test_uses_compile_settings_not_cut_settings(self):
        argv = compile_stage._segment_argv(
            "ffmpeg",
            Path("in.mp4"),
            Path("out.mp4"),
            10.0,
            20.0,
            _settings({"compile.preset": "fast", "cut.preset": "veryfast"}),
        )
        self.assertIn("fast", argv)
        self.assertNotIn("veryfast", argv)


class TestConcatList(unittest.TestCase):
    def test_writes_one_file_line_per_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            list_path = compile_stage._write_concat_list(
                scratch, ["001-abc.mp4", "002-def.mp4"]
            )
            text = list_path.read_text(encoding="utf-8")
            self.assertIn("ffconcat version 1.0", text)
            self.assertIn("file 001-abc.mp4", text)
            self.assertIn("file 002-def.mp4", text)


def _fake_run_ffmpeg(argv, *args, **kwargs):
    # Mirrors real ffmpeg's contract for our purposes: the output path is
    # always the last argv element, so drop a non-empty placeholder there.
    # Signature-agnostic (*args/**kwargs) since this stands in for
    # _run_ffmpeg's (argv, total_seconds, progress, base, span, log_path,
    # out_path) - none of those extra positional args matter to a fake that
    # never actually reads ffmpeg's progress output.
    out_path = Path(argv[-1])
    out_path.write_bytes(b"fake-mp4-bytes")


class TestRenderCompilation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws").ensure()
        (self.ws.root / "video.mp4").write_bytes(b"fake-source")
        self.ws.write_state({"video_file": "video.mp4", "duration": 1000.0})
        self.settings = _settings()

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_compilation_raises(self):
        with self.assertRaises(StageError):
            compile_stage.render_compilation(self.ws, self.settings, "nope")

    def test_segment_shorter_than_min_duration_raises(self):
        # Save under a permissive min_duration so the short segment can even
        # be stored - render_compilation must still catch it independently
        # once padding is applied, since padding can be clamped away near a
        # VOD boundary and this is the last line of defense against that.
        comp.upsert(
            self.ws,
            "tiny",
            [{"start": 10.0, "end": 10.1}],
            _settings({"compile.min_duration": 0.0}),
        )
        settings = _settings({"cut.pad_start": 0.0, "cut.pad_end": 0.0})
        with mock.patch.object(compile_stage, "resolve_tool", return_value="ffmpeg-stub"), \
             mock.patch.object(compile_stage, "_run_ffmpeg", side_effect=_fake_run_ffmpeg):
            with self.assertRaises(StageError):
                compile_stage.render_compilation(self.ws, settings, "tiny")

    def test_renders_and_records_output(self):
        comp.upsert(
            self.ws,
            "catan-highlights",
            [
                {"start": 100.0, "end": 110.0, "label": "one"},
                {"start": 200.0, "end": 210.0, "label": "two"},
            ],
            self.settings,
        )
        with mock.patch.object(compile_stage, "resolve_tool", return_value="ffmpeg-stub"), \
             mock.patch.object(compile_stage, "_run_ffmpeg", side_effect=_fake_run_ffmpeg):
            out_path = compile_stage.render_compilation(
                self.ws, self.settings, "catan-highlights"
            )

        self.assertTrue(out_path.exists())
        self.assertEqual(out_path, self.ws.compilations_dir / "catan-highlights.mp4")

        doc = comp.load(self.ws)
        rendered = comp.get(doc, "catan-highlights")
        self.assertIsNotNone(rendered["output"])
        self.assertEqual(rendered["output"]["file"], "clips/compilations/catan-highlights.mp4")
        self.assertGreater(rendered["output"]["bytes"], 0)
        # Two 10s segments, padded by cut.pad_start=1.0/pad_end=1.5 -> 12.5s each.
        self.assertAlmostEqual(rendered["output"]["duration"], 25.0, places=2)

        self.assertTrue(self.ws.stage_done("compile"))

    def test_unchanged_compilation_skips_render(self):
        comp.upsert(self.ws, "x", [{"start": 100.0, "end": 110.0}], self.settings)
        calls = []

        def counting_run_ffmpeg(argv, *args, **kwargs):
            calls.append(argv)
            _fake_run_ffmpeg(argv, *args, **kwargs)

        with mock.patch.object(compile_stage, "resolve_tool", return_value="ffmpeg-stub"), \
             mock.patch.object(compile_stage, "_run_ffmpeg", side_effect=counting_run_ffmpeg):
            compile_stage.render_compilation(self.ws, self.settings, "x")
            first_call_count = len(calls)
            compile_stage.render_compilation(self.ws, self.settings, "x")

        # Second render should skip entirely: no new ffmpeg invocations.
        self.assertEqual(len(calls), first_call_count)

    def test_force_re_renders_even_when_unchanged(self):
        comp.upsert(self.ws, "x", [{"start": 100.0, "end": 110.0}], self.settings)
        calls = []

        def counting_run_ffmpeg(argv, *args, **kwargs):
            calls.append(argv)
            _fake_run_ffmpeg(argv, *args, **kwargs)

        with mock.patch.object(compile_stage, "resolve_tool", return_value="ffmpeg-stub"), \
             mock.patch.object(compile_stage, "_run_ffmpeg", side_effect=counting_run_ffmpeg):
            compile_stage.render_compilation(self.ws, self.settings, "x")
            before = len(calls)
            compile_stage.render_compilation(self.ws, self.settings, "x", force=True)

        # force=True re-runs the final concat join at least (segment cut is
        # itself skipped via the on-disk scratch file, same as cut.py).
        self.assertGreater(len(calls), before)


class TestCrossStreamRender(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = Workspace(root / "home").ensure()
        (self.home.root / "video.mp4").write_bytes(b"fake-home-source")
        self.home.write_state({"video_file": "video.mp4", "duration": 1000.0})

        self.other = Workspace(root / "other").ensure()
        (self.other.root / "video.mp4").write_bytes(b"fake-other-source")
        self.other.write_state({"video_file": "video.mp4", "duration": 2000.0})

        # work_root must be absolute here - Settings.work_root resolves a
        # relative one against the real project root, not this temp dir.
        self.settings = _settings({"work_root": str(root)})

    def tearDown(self):
        self._tmp.cleanup()

    def test_renders_segments_from_multiple_workspaces(self):
        comp.upsert(
            self.home,
            "cross",
            [
                {"start": 100.0, "end": 110.0, "label": "local"},
                {"start": 300.0, "end": 310.0, "label": "foreign", "slug": "other"},
            ],
            self.settings,
        )
        seen_sources = []

        def recording_run_ffmpeg(argv, *args, **kwargs):
            if "-i" in argv:
                seen_sources.append(argv[argv.index("-i") + 1])
            _fake_run_ffmpeg(argv, *args, **kwargs)

        with mock.patch.object(compile_stage, "resolve_tool", return_value="ffmpeg-stub"), \
             mock.patch.object(compile_stage, "_run_ffmpeg", side_effect=recording_run_ffmpeg):
            out_path = compile_stage.render_compilation(self.home, self.settings, "cross")

        self.assertTrue(out_path.exists())
        self.assertIn(str(self.home.root / "video.mp4"), seen_sources)
        self.assertIn(str(self.other.root / "video.mp4"), seen_sources)

    def test_missing_foreign_workspace_raises_clear_error(self):
        comp.upsert(
            self.home,
            "cross",
            [{"start": 100.0, "end": 110.0, "slug": "does-not-exist"}],
            self.settings,
        )
        with mock.patch.object(compile_stage, "resolve_tool", return_value="ffmpeg-stub"), \
             mock.patch.object(compile_stage, "_run_ffmpeg", side_effect=_fake_run_ffmpeg):
            with self.assertRaises(StageError) as ctx:
                compile_stage.render_compilation(self.home, self.settings, "cross")
        self.assertIn("does-not-exist", str(ctx.exception))


class TestUnrenderedCompilationsElsewhere(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.a = Workspace(root / "a").ensure()
        self.b = Workspace(root / "b").ensure()
        self.settings = _settings({"work_root": str(root)})

    def tearDown(self):
        self._tmp.cleanup()

    def test_unrendered_foreign_compilation_blocks_source_cleanup(self):
        # Compilation lives in b's compilations.json but pulls footage from a.
        comp.upsert(
            self.b, "x", [{"start": 0.0, "end": 5.0, "slug": "a"}], self.settings
        )
        pending = compile_stage.unrendered_compilations_elsewhere(self.a, self.settings)
        self.assertEqual([(p["workspace"], p["compilation"]) for p in pending], [("b", "x")])

    def test_rendered_foreign_compilation_does_not_block(self):
        comp.upsert(
            self.b, "x", [{"start": 0.0, "end": 5.0, "slug": "a"}], self.settings
        )
        comp.set_output(self.b, "x", {"file": "clips/compilations/x.mp4"})
        self.b.compilations_dir.mkdir(parents=True, exist_ok=True)
        (self.b.compilations_dir / "x.mp4").write_bytes(b"fake")
        self.assertEqual(
            compile_stage.unrendered_compilations_elsewhere(self.a, self.settings), []
        )

    def test_own_workspace_excluded_from_scan(self):
        # a's own unrendered compilations are covered by unrendered_compilations(a)
        # already - unrendered_compilations_elsewhere must not double-report them.
        comp.upsert(self.a, "x", [{"start": 0.0, "end": 5.0}], self.settings)
        self.assertEqual(
            compile_stage.unrendered_compilations_elsewhere(self.a, self.settings), []
        )

    def test_unrelated_foreign_compilation_does_not_block(self):
        # b's compilation only uses b's own footage - irrelevant to a's cleanup.
        comp.upsert(self.b, "x", [{"start": 0.0, "end": 5.0}], self.settings)
        self.assertEqual(
            compile_stage.unrendered_compilations_elsewhere(self.a, self.settings), []
        )


class TestUnrenderedCompilations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws").ensure()
        self.settings = _settings()

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_compilations_is_empty(self):
        self.assertEqual(compile_stage.unrendered_compilations(self.ws), [])

    def test_never_rendered_is_pending(self):
        comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], self.settings)
        pending = compile_stage.unrendered_compilations(self.ws)
        self.assertEqual([c["name"] for c in pending], ["x"])

    def test_rendered_with_existing_file_is_not_pending(self):
        comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], self.settings)
        self.ws.compilations_dir.mkdir(parents=True, exist_ok=True)
        (self.ws.compilations_dir / "x.mp4").write_bytes(b"fake")
        comp.set_output(self.ws, "x", {"file": "clips/compilations/x.mp4"})
        self.assertEqual(compile_stage.unrendered_compilations(self.ws), [])

    def test_rendered_but_file_deleted_is_pending_again(self):
        comp.upsert(self.ws, "x", [{"start": 0.0, "end": 5.0}], self.settings)
        comp.set_output(self.ws, "x", {"file": "clips/compilations/x.mp4"})
        # Output metadata says rendered, but the file itself is gone.
        pending = compile_stage.unrendered_compilations(self.ws)
        self.assertEqual([c["name"] for c in pending], ["x"])


if __name__ == "__main__":
    unittest.main()
