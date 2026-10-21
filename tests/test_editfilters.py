"""Tests for clipbot/editfilters.py.

Three layers: golden fragments (what a builder *says*), numeric checks (the easing
expressions ffmpeg will evaluate agree with `editspec.ease_value`, which is the
curve the spec author reasons about), and - when an ffmpeg is on this machine - a
smoke test that a built clip graph parses, runs, and produces exactly the planned
number of frames with picture and sound the same length.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from clipbot import editfilters as F
from clipbot import editspec as E
from clipbot import edittimeline as T

SOURCES = {"v": {"file": "D:/x.mp4"}, "w": {"file": "D:/y.mp4"}}
RAMP = [{"at": 11, "rate": 1.0}, {"at": 11.5, "rate": 0.5}, {"at": 12.5, "rate": 0.5}, {"at": 13, "rate": 1.0}]


def spec(clips, **extra):
    d = {"name": "t", "sources": SOURCES, "clips": clips}
    d.update(extra)
    return E.normalize(d)


def evaluate(expr, **names):
    """Evaluate an ffmpeg expression in Python: the subset the builders emit."""
    py = re.sub(r"\bif\(", "_if(", expr)
    env = {"_if": lambda c, a, b: a if c else b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
           "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "pow": pow, "min": min, "max": max,
           "clip": lambda x, lo, hi: min(max(x, lo), hi), "between": lambda x, a, b: a <= x <= b}
    env.update(names)
    return eval(py, {"__builtins__": {}}, env)


class TestNumbers(unittest.TestCase):
    def test_num_is_compact_and_locale_proof(self):
        self.assertEqual(F.num(1.0), "1")
        self.assertEqual(F.num(0.12345), "0.1235")
        self.assertEqual(F.num(-0.00001), "0")
        self.assertEqual(F.num(12.5), "12.5")

    def test_q_quotes(self):
        self.assertEqual(F.q("between(t,1,2)"), "'between(t,1,2)'")


class TestEasingExpressions(unittest.TestCase):
    def test_every_ease_matches_the_python_curve(self):
        for kind in E.EASES:
            expr = F.ease_expr(kind, "p")
            for p in (-0.5, 0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0, 1.5):
                self.assertAlmostEqual(evaluate(expr, p=p), E.ease_value(kind, p), places=9, msg="{0} at {1}".format(kind, p))

    def test_unknown_ease(self):
        with self.assertRaises(E.SpecError):
            F.ease_expr("bounce", "p")

    def test_back_out_overshoots_then_lands(self):
        expr = F.back_out_expr("p")
        self.assertAlmostEqual(evaluate(expr, p=0.0), 0.0, places=3)
        self.assertAlmostEqual(evaluate(expr, p=1.0), 1.0, places=3)
        self.assertGreater(max(evaluate(expr, p=i / 100.0) for i in range(101)), 1.05)

    def test_keyed_expression_hits_keys_and_eases_between(self):
        keys = [(1.0, 1.0, "linear"), (2.0, 1.2, "out2"), (3.0, 1.2, "linear"), (4.0, 1.0, "inout2")]
        expr = F.keyed_expr(keys)
        for t, v, _ in keys:
            self.assertAlmostEqual(evaluate(expr, t=t), v, places=6)
        self.assertAlmostEqual(evaluate(expr, t=0.0), 1.0)           # before the first key: held
        self.assertAlmostEqual(evaluate(expr, t=9.0), 1.0)           # after the last: held
        want = 1.0 + 0.2 * E.ease_value("out2", 0.5)
        self.assertAlmostEqual(evaluate(expr, t=1.5), want, places=6)

    def test_single_key_is_a_constant(self):
        self.assertEqual(F.keyed_expr([(1.0, 1.25, "linear")]), "1.25")

    def test_no_keys(self):
        with self.assertRaises(E.SpecError):
            F.keyed_expr([])


class TestAudioRetiming(unittest.TestCase):
    def test_atempo_chains_below_half_speed(self):
        self.assertEqual(F.atempo_chain(0.25), "atempo=0.5,atempo=0.5")
        self.assertEqual(F.atempo_chain(0.5), "atempo=0.5")
        self.assertEqual(F.atempo_chain(1.0), "atempo=1")

    def test_every_instance_is_within_ffmpegs_limits_and_the_product_is_the_rate(self):
        for rate in (0.1, 0.25, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0):
            parts = [float(x) for x in re.findall(r"atempo=([\d.]+)", F.atempo_chain(rate))]
            self.assertTrue(all(0.5 <= p <= 2.0 for p in parts), rate)
            prod = 1.0
            for p in parts:
                prod *= p
            self.assertAlmostEqual(prod, rate, places=3, msg=str(rate))

    def _pieces(self, **kw):
        c = spec([{"id": "a", "src": "v", "in": 10, "out": 14, "speed": RAMP, **kw}])["clips"][0]
        return T.build_pieces(c)

    def test_a_ramp_is_one_audio_group_not_thirty_stitches(self):
        groups = F.audio_groups(self._pieces(), "tempo")
        self.assertEqual([g["kind"] for g in groups], ["plain", "ramp", "plain"])

    def test_group_durations_sum_to_the_picture(self):
        pieces = self._pieces()
        groups = F.audio_groups(pieces, "tempo")
        self.assertAlmostEqual(sum(g["dur"] for g in groups), sum(p["out_dur"] for p in pieces), places=6)

    def test_pitch_mode_keeps_every_piece(self):
        pieces = self._pieces()
        groups = F.audio_groups(pieces, "pitch")
        self.assertEqual(sum(1 for g in groups if g["kind"] == "pitch"), sum(1 for p in pieces if abs(p["rate"] - 1) > 1e-9))

    def test_mute_turns_slowed_pieces_to_silence(self):
        groups = F.audio_groups(self._pieces(), "mute")
        self.assertIn("silence", [g["kind"] for g in groups])
        self.assertNotIn("ramp", [g["kind"] for g in groups])

    def test_a_freeze_is_silent(self):
        c = spec([{"id": "a", "src": "v", "in": 10, "out": 20, "freezes": [{"at": 15, "dur": 1.0}]}])["clips"][0]
        groups = F.audio_groups(T.build_pieces(c), "tempo")
        self.assertEqual([g["kind"] for g in groups], ["plain", "silence", "plain"])


class TestAssembly(unittest.TestCase):
    def _timeline(self, transition):
        return T.compile_timeline(spec([
            {"id": "a", "src": "v", "in": 10, "out": 14},
            {"id": "b", "src": "w", "in": 0, "out": 4, "transition_in": transition}]))

    def test_cuts_need_no_graph(self):
        tl = self._timeline({"type": "cut"})
        self.assertFalse(F.needs_filter_assembly(tl["clips"]))
        self.assertEqual(F.audio_assembly(tl["clips"])[1].count("["), F.audio_assembly(tl["clips"])[1].count("]"))

    def test_a_crossfade_needs_the_graph(self):
        tl = self._timeline({"type": "xfade", "name": "fade", "dur": 0.5})
        self.assertTrue(F.needs_filter_assembly(tl["clips"]))
        graph, label = F.video_assembly(tl["clips"], 60.0)
        self.assertIn("xfade=transition=fade:duration=0.5:offset=3.5", graph)
        self.assertIn("settb=AVTB", graph)            # xfade refuses inputs with different timebases
        self.assertTrue(graph.rstrip().endswith("[{0}]".format(label)))
        self.assertIn("trim=end_frame=", graph)       # and one frame too many without this

    def test_audio_crossfades_match_the_picture_overlap(self):
        tl = self._timeline({"type": "xfade", "name": "fade", "dur": 0.5})
        graph, _ = F.audio_assembly(tl["clips"])
        self.assertIn("acrossfade=d=0.5", graph)


class TestMaster(unittest.TestCase):
    MEASURED = {"input_i": -20.0, "input_tp": -3.0, "input_lra": 8.0, "input_thresh": -30.0, "target_offset": 0.3}

    def test_second_pass_applies_the_measurement_linearly(self):
        m = spec([{"id": "a", "src": "v", "in": 10, "out": 20}])["master"]
        f = F.master_filters(m, self.MEASURED, 20.0)
        self.assertIn("measured_I=-20", f)
        self.assertIn("linear=true", f)
        self.assertIn("alimiter", f)
        self.assertIn("afade=t=out:st=19.4", f)

    def test_the_limiter_ceiling_leaves_room_for_the_encoder(self):
        m = spec([{"id": "a", "src": "v", "in": 10, "out": 20}])["master"]
        limit = float(re.search(r"alimiter=limit=([\d.]+)", F.master_filters(m, self.MEASURED, 20.0)).group(1))
        import math
        self.assertLessEqual(20 * math.log10(limit), m["true_peak_db"] - F.LIMITER_MARGIN_DB + 0.01)

    def test_normalisation_can_be_off(self):
        m = dict(spec([{"id": "a", "src": "v", "in": 10, "out": 20}])["master"], normalize=False)
        self.assertNotIn("loudnorm", F.master_filters(m, None, 20.0))

    def test_without_a_measurement_it_falls_back_to_dynamic(self):
        m = spec([{"id": "a", "src": "v", "in": 10, "out": 20}])["master"]
        f = F.master_filters(m, None, 20.0)
        self.assertIn("loudnorm=I=-14", f)
        self.assertNotIn("linear=true", f)

    def test_analysis_pass_prints_json(self):
        m = spec([{"id": "a", "src": "v", "in": 10, "out": 20}])["master"]
        self.assertIn("print_format=json", F.loudnorm_analysis(m))


class TestMix(unittest.TestCase):
    def _items(self):
        sp = spec([{"id": "a", "src": "v", "in": 10, "out": 20}],
                  tracks={"music": [{"id": "bed", "asset": "mus_3a25c534c578d312"}],
                          "sfx": [{"id": "s", "asset": "sfx_08a073fd8c00c828", "on": {"clip": "a", "at": 12}}]})
        tl = T.compile_timeline(sp)
        music = [dict(tl["tracks"]["music"][0], index=1, path="m.wav")]
        sfx = [dict(tl["tracks"]["sfx"][0], index=2, path="s.wav", start=2.0, skip=0.0, take=0.5)]
        return music, sfx, tl["duration"]

    def test_music_is_ducked_by_the_game_audio(self):
        music, sfx, total = self._items()
        graph, _ = F.mix_graph(music, sfx, total, 48000)
        self.assertIn("sidechaincompress", graph)

    def test_mix_does_not_renormalise(self):
        music, sfx, total = self._items()
        graph, _ = F.mix_graph(music, sfx, total, 48000)
        self.assertIn("normalize=0", graph)       # amix would otherwise halve every input

    def test_sfx_lands_where_asked(self):
        music, sfx, total = self._items()
        graph, _ = F.mix_graph([], sfx, total, 48000)
        self.assertIn("adelay=2000", graph)

    def test_input_numbers_are_used(self):
        music, sfx, total = self._items()
        graph, _ = F.mix_graph(music, sfx, total, 48000)
        self.assertIn("[1:a]", graph)
        self.assertIn("[2:a]", graph)


class TestCanvas(unittest.TestCase):
    def test_matching_size_is_only_normalised(self):
        g = F.Graph()
        F.canvas_chain(g, "0:v", (1920, 1080), (1920, 1080), "fit")
        self.assertEqual(g.render(), "[0:v]setsar=1[cv1]")

    def test_fit_letterboxes(self):
        g = F.Graph()
        F.canvas_chain(g, "0:v", (1280, 720), (1080, 1920), "fit")
        self.assertIn("force_original_aspect_ratio=decrease", g.render())
        self.assertIn("pad=1080:1920", g.render())

    def test_fill_crops_around_the_focus(self):
        g = F.Graph()
        F.canvas_chain(g, "0:v", (1280, 720), (1080, 1920), "fill", focus={"x": 0.3, "y": 0.5})
        self.assertIn("0.3*iw", g.render())

    def test_blur_puts_the_picture_over_a_blurred_copy(self):
        g = F.Graph()
        F.canvas_chain(g, "0:v", (1280, 720), (1080, 1920), "blur")
        text = g.render()
        self.assertIn("split=2", text)
        self.assertIn("gblur", text)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", text)

    def test_crop_is_applied_first_and_made_even(self):
        g = F.Graph()
        F.canvas_chain(g, "0:v", (1280, 720), (1080, 1920), "fit", crop={"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0})
        self.assertTrue(g.render().startswith("[0:v]crop=640:720:320:0"))

    def test_unknown_mode(self):
        with self.assertRaises(E.SpecError):
            F.canvas_chain(F.Graph(), "0:v", (1280, 720), (1080, 1920), "stretch")


class TestGrade(unittest.TestCase):
    def test_none_does_nothing(self):
        self.assertIsNone(F.grade_filters({"preset": "none"}))

    def test_strength_scales_the_look(self):
        subtle = F.grade_filters({"preset": "warm_lift", "strength": "subtle"})
        strong = F.grade_filters({"preset": "warm_lift", "strength": "strong"})
        sat = lambda s: float(re.search(r"saturation=([\d.]+)", s).group(1))
        self.assertLess(sat(subtle), sat(strong))


def _ffmpeg():
    try:
        from clipbot.config import load_settings
        from clipbot.utils import resolve_tool
        s = load_settings()
        return resolve_tool(s.tool("ffmpeg"), ""), resolve_tool(s.tool("ffprobe"), "")
    except Exception:  # noqa: BLE001 - no ffmpeg here: the smoke test is skipped, not failed
        w = shutil.which("ffmpeg"), shutil.which("ffprobe")
        return w if all(w) else None


class TestClipGraphWithRealFfmpeg(unittest.TestCase):
    """A built graph must parse and run, and the clip must be exactly as long as planned."""

    @classmethod
    def setUpClass(cls):
        cls.tools = _ffmpeg()
        if not cls.tools:
            raise unittest.SkipTest("ffmpeg is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = str(Path(cls.tmp.name) / "src.mp4")
        subprocess.run([cls.tools[0], "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=8",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
                        "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.src], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, clip):
        sp = spec([clip], output={"width": 320, "height": 180, "fps": 30, "preset": "preview_540p"})
        tl = T.compile_timeline(sp)
        entry, c = tl["clips"][0], sp["clips"][0]
        seg = {"path": self.src, "in_rel": 0.0, "w": 320, "h": 180, "has_audio": True, "tag": "t", "key": "k"}
        cg = F.build_clip_graph(c, entry, sp["output"], seg, head=None, tail=None, overlay_pngs=[], assets={}, styles={},
                                fallback_font=None)
        script = Path(self.tmp.name) / "g.txt"
        script.write_text(cg["filter"], encoding="utf-8")
        vout, aout = Path(self.tmp.name) / "o.mp4", Path(self.tmp.name) / "o.wav"
        argv = [self.tools[0], "-hide_banner", "-loglevel", "error", "-y"]
        for inp in cg["inputs"]:
            argv += inp
        argv += ["-/filter_complex", str(script), "-map", cg["vmap"], "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(vout),
                 "-map", cg["amap"], "-c:a", "pcm_s16le", str(aout)]
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        return entry, vout, aout

    def _count(self, path):
        out = subprocess.run([self.tools[1], "-v", "error", "-count_frames", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                             stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        return int(out)

    def _duration(self, path):
        out = subprocess.run([self.tools[1], "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        return float(out)

    def test_plain_clip(self):
        entry, v, a = self._run({"id": "a", "src": "v", "in": 1, "out": 4})
        self.assertEqual(self._count(v), entry["frames"])
        self.assertAlmostEqual(self._duration(a), entry["dur"], delta=0.03)

    def test_slow_motion_ramp_with_camera_and_flash(self):
        entry, v, a = self._run({
            "id": "a", "src": "v", "in": 1, "out": 6, "speed": [
                {"at": 2, "rate": 1.0}, {"at": 2.5, "rate": 0.5}, {"at": 3.5, "rate": 0.5}, {"at": 4, "rate": 1.0}],
            "camera": [{"at": 2, "zoom": 1.0}, {"at": 3, "zoom": 1.2, "ease": "out2"}],
            "fx": [{"type": "flash", "at": 3, "dur": 0.1}], "grade": {"preset": "gamer_pop"}})
        self.assertEqual(self._count(v), entry["frames"])
        self.assertAlmostEqual(self._duration(a), entry["dur"], delta=0.03)
        self.assertGreater(entry["dur"], 5.5)

    def test_freeze(self):
        entry, v, a = self._run({"id": "a", "src": "v", "in": 1, "out": 4, "freezes": [{"at": 2, "dur": 1.0}]})
        self.assertEqual(self._count(v), entry["frames"])
        self.assertAlmostEqual(self._duration(a), entry["dur"], delta=0.03)


if __name__ == "__main__":
    unittest.main()
