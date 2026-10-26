"""Motion graphics: the spec kind, its timing, the filter graph, the render job's key, and (when Node, Chrome and
ffmpeg are all there) a real Remotion render laid over a real clip."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clipbot import editfilters as F
from clipbot import editmotion as M
from clipbot import editscene
from clipbot import editspec as E
from clipbot import edittimeline as T
from clipbot.config import Settings, load_settings
from clipbot.specerror import SpecError
from clipbot.stages import edit as stage
from clipbot.utils import resolve_tool

ROOT_JSX = (M.GRAPHICS_DIR / "src" / "Root.jsx").read_text(encoding="utf-8")


def clip_spec(overlays, out_dur=8.0, extra_clips=()):
    return {"name": "m", "profile": "balanced", "output": {"preset": "youtube_1080p30"},
            "sources": {"v": {"file": "C:/x.mp4"}},
            "clips": [{"id": "a", "src": "v", "in": 10.0, "out": 10.0 + out_dur, "overlays": overlays}] + list(extra_clips)}


def probe(at, **kw):
    return dict({"kind": "motion", "comp": "Probe", "at": at}, **kw)


class TestCatalogue(unittest.TestCase):
    def test_every_composition_in_root_jsx_is_in_comps_with_the_same_defaults(self):
        found = {m.group(1): (float(m.group(2)), float(m.group(3))) for m in
                 re.finditer(r"\{def\('(\w+)',\s*\w+,\s*([\d.]+),\s*([\d.]+)", ROOT_JSX)}
        self.assertEqual(set(found), set(M.COMPS))
        for comp, (dur, hit) in found.items():
            self.assertEqual((M.COMPS[comp]["dur"], M.COMPS[comp]["hit"]), (dur, hit), comp)

    def test_root_registers_every_composition_with_no_demo_props(self):
        """Remotion merges a composition's defaultProps under a render's props, so a demo victim or distance leaks onto a
        real kill that has none (a plate said KHANNY on four kills). Every `def(...)` call is exactly four arguments."""
        calls = re.findall(r"\{def\(([^}]*)\)\}", ROOT_JSX)
        self.assertEqual(len(calls), len(M.COMPS))
        for c in calls:
            self.assertEqual(len([a for a in c.split(",") if a.strip()]), 4, c)
        self.assertEqual(ROOT_JSX.count("defaultProps="), 1)
        self.assertIn("defaultProps={{dur, hit, fps: 60}}", ROOT_JSX)           # nothing but the clock: no demo data

    def test_every_composition_file_exists(self):
        for comp in M.COMPS:
            self.assertTrue((M.GRAPHICS_DIR / "src" / "comps" / (comp + ".jsx")).is_file(), comp)


class TestSpec(unittest.TestCase):
    def norm(self, overlays, **kw):
        return E.normalize(clip_spec(overlays, **kw))["clips"][0]["overlays"]

    def test_defaults_come_from_the_catalogue(self):
        (o,) = self.norm([probe(14.0)])
        self.assertEqual((o["dur"], o["hit"]), (M.COMPS["Probe"]["dur"], M.COMPS["Probe"]["hit"]))
        self.assertEqual((o["anim_in"], o["anim_out"], o["x"], o["y"]), ("none", "none", 0.5, 0.5))

    def test_it_survives_a_round_trip_through_json(self):
        a = E.normalize(clip_spec([probe(14.0, hit=0.4, dur=2.0, props={"text": "HI", "n": [1, 2]}, id="m1")]))
        self.assertEqual(E.normalize(__import__("json").loads(E.to_json(a))), a)

    def test_an_unknown_composition_is_refused_and_the_known_ones_are_named(self):
        with self.assertRaises(SpecError) as cm:
            self.norm([probe(14.0, comp="Nope")])
        self.assertIn("Title", str(cm.exception))
        self.assertIn(".comp", str(cm.exception))

    def test_props_must_be_json_and_not_huge(self):
        with self.assertRaises(SpecError):
            self.norm([probe(14.0, props={"x": object()})])
        with self.assertRaises(SpecError):
            self.norm([probe(14.0, props={"x": "y" * (M.MAX_PROPS_CHARS + 10)})])
        with self.assertRaises(SpecError):
            self.norm([probe(14.0, props=[1])])

    def test_a_graphic_places_and_animates_itself(self):
        for k, v in (("x", 0.3), ("y", 0.2), ("anim_in", "pop"), ("w", 0.5)):
            with self.assertRaises(SpecError, msg=k):
                self.norm([probe(14.0, **{k: v})])

    def test_the_hit_cannot_be_after_the_end(self):
        with self.assertRaises(SpecError):
            self.norm([probe(14.0, hit=2.0, dur=1.0)])

    def test_the_landing_must_be_inside_the_clip(self):
        with self.assertRaises(SpecError) as cm:
            self.norm([probe(30.0)])
        self.assertIn("source window", str(cm.exception))

    def test_motion_fields_on_another_overlay_kind_are_refused(self):
        with self.assertRaises(SpecError):
            self.norm([{"kind": "text", "at": 12.0, "text": "x", "comp": "Probe"}])


class TestTimeline(unittest.TestCase):
    def entry(self, overlays, **kw):
        return T.compile_timeline(E.normalize(clip_spec(overlays, **kw)))["clips"][0]

    def test_the_graphic_starts_hit_seconds_before_the_landing(self):
        (o,) = self.entry([probe(14.0, hit=0.5, dur=1.0)])["overlays"]
        self.assertAlmostEqual(o["t0"], 4.0 - 0.5, places=3)
        self.assertAlmostEqual(o["t1"], 4.0 + 0.5, places=3)
        self.assertEqual(o["trim"], 0.0)

    def test_a_landing_near_the_start_trims_the_front_of_the_graphic(self):
        (o,) = self.entry([probe(10.2, hit=0.5, dur=1.0)])["overlays"]
        self.assertEqual(o["t0"], 0.0)
        self.assertAlmostEqual(o["trim"], 0.3, places=3)
        self.assertAlmostEqual(o["t1"], 0.7, places=3)

    def test_a_slow_motion_ramp_moves_the_landing_but_not_the_length(self):
        spec = clip_spec([probe(14.0, hit=0.5, dur=1.0)])
        spec["clips"][0]["speed"] = [{"at": 12.0, "rate": 1.0}, {"at": 13.0, "rate": 0.5}, {"at": 16.0, "rate": 0.5}]
        (o,) = T.compile_timeline(E.normalize(spec))["clips"][0]["overlays"]
        landing = o["t0"] + 0.5
        self.assertGreater(landing, 4.0 + 0.3)           # the clip is longer before the kill than it was
        self.assertAlmostEqual(o["t1"] - o["t0"], 1.0, places=3)

    def test_a_graphic_that_falls_off_the_end_is_dropped_with_a_warning(self):
        spec = clip_spec([probe(18.0, hit=0.2, dur=1.0)], out_dur=8.0)
        e = T.compile_timeline(E.normalize(spec))["clips"][0]
        self.assertEqual(e["overlays"][0]["kind"], "motion")                       # 17.8 .. 18.0, inside the clip
        spec = clip_spec([probe(10.0, hit=0.0, dur=0.3)])
        spec["clips"][0]["out"] = 10.5
        self.assertTrue(T.compile_timeline(E.normalize(spec))["clips"][0]["overlays"])

    def test_the_hit_and_a_beat_every_two_seconds_count_as_events(self):
        tl = T.compile_timeline(E.normalize(clip_spec([probe(14.0, hit=0.5, dur=5.0)])))
        labels = [e["label"] for e in tl["events"] if e["kind"] == "overlay"]
        self.assertTrue(any(l.endswith(":hit") for l in labels))
        self.assertGreaterEqual(sum(1 for l in labels if l.endswith(":live")), 1)

    def test_pacing_lets_a_graphic_stay_longer_than_a_still(self):
        long_still = {"kind": "text", "at": 12.0, "dur": 7.0, "text": "HOLD"}
        issues = lambda ov: [i for i in T.pacing(T.compile_timeline(E.normalize(clip_spec(ov, out_dur=20.0))))["issues"] if i["what"] == "dwell"]
        self.assertTrue(issues([long_still]))
        self.assertFalse(issues([probe(14.0, hit=0.5, dur=7.0)]))
        self.assertTrue(issues([probe(14.0, hit=0.5, dur=9.0)]))


class TestFilters(unittest.TestCase):
    def item(self, **kw):
        base = {"kind": "motion", "index": 2, "t0": 3.5, "t1": 4.5, "x": 0.5, "y": 0.5, "anchor": "center", "anim_in": "none",
                "anim_out": "none", "in_dur": 0.0, "out_dur": 0.0}
        return dict(base, **kw)

    def test_the_overlay_is_shifted_scaled_and_lets_the_picture_through_after_it_ends(self):
        g = F.Graph()
        F.overlay_chains(g, "v0", [self.item()], (1920, 1080))
        text = g.render()
        self.assertIn("[2:v]scale=1920:1080", text)
        self.assertIn("setpts=PTS-STARTPTS+3.5/TB", text)
        self.assertIn("eof_action=pass", text)
        self.assertIn("between(t,3.5,4.5)", text)

    def test_the_clip_graph_trims_the_front_of_a_graphic_with_ss_before_its_input(self):
        spec = E.normalize(clip_spec([probe(10.2, hit=0.5, dur=1.0)]))
        entry = T.compile_timeline(spec)["clips"][0]
        clip = spec["clips"][0]
        out = spec["output"]
        cg = F.build_clip_graph(clip, entry, out, {"path": "C:/x.mp4", "in_rel": 0.6, "w": 1920, "h": 1080, "has_audio": True},
                                overlay_pngs=[{"path": "C:/motion/k.mov", "w": 1920, "h": 1080, "key": "k", "motion": True}])
        args = next(a for a in cg["inputs"] if "C:/motion/k.mov" in a)
        self.assertEqual(args[:2], ["-ss", "0.3"])
        self.assertEqual(args[-2:], ["-i", "C:/motion/k.mov"])
        self.assertNotIn("-loop", args)


class TestJob(unittest.TestCase):
    base = {"kind": "motion", "comp": "Probe", "props": {"text": "A"}, "dur": 1.0, "hit": 0.5}

    def key(self, fps=60.0, width=1920, **kw):
        return M.make_job(dict(self.base, **kw), fps, width)["key"]

    def test_the_key_follows_everything_the_picture_depends_on(self):
        k = self.key()
        self.assertEqual(k, self.key())
        for changed in (self.key(props={"text": "B"}), self.key(dur=1.5), self.key(hit=0.4), self.key(fps=30.0), self.key(width=960),
                        self.key(comp="Title")):
            self.assertNotEqual(k, changed)

    def test_the_scale_is_what_makes_a_preview_cheap(self):
        self.assertEqual(M.scale_for(1920), 1.0)
        self.assertEqual(M.scale_for(960), 0.5)
        self.assertEqual(M.scale_for(320), 0.25)           # 1/6 would not give whole pixels: Remotion refuses it
        self.assertEqual(M.scale_for(1280), 1.0)
        self.assertEqual(M.scale_for(4000), 1.0)

    def test_an_edited_image_in_the_props_changes_the_key_though_its_path_did_not(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "icon.png"
            p.write_bytes(b"one")
            a = self.key(props={"icon": "file:" + str(p)})
            p.write_bytes(b"two!!")
            self.assertNotEqual(a, self.key(props={"icon": "file:" + str(p)}))

    def test_editing_a_composition_changes_every_key(self):
        before = M.source_hash()
        f = M.GRAPHICS_DIR / "src" / "comps" / "Probe.jsx"
        text = f.read_bytes()
        M.source_hash.cache_clear()
        try:
            f.write_bytes(text + b"\n// edited\n")
            self.assertNotEqual(before, M.source_hash.__wrapped__())
        finally:
            f.write_bytes(text)
            M.source_hash.cache_clear()

    def test_missing_node_is_a_clear_error(self):
        s = Settings({"tools": {"node": "C:/definitely/not/node.exe"}}, Path("s.json"))
        problems = M.check_ready(s)
        self.assertTrue(any("Node 22+" in p for p in problems))


def _ready():
    try:
        s = load_settings()
        if M.check_ready(s):
            return None
        return s, resolve_tool(s.tool("ffmpeg"), ""), resolve_tool(s.tool("ffprobe"), "")
    except Exception:  # noqa: BLE001
        return None


class TestRealRender(unittest.TestCase):
    """Remotion draws a Probe (a solid amber bar at a known place) and the engine lays it over a real clip."""

    @classmethod
    def setUpClass(cls):
        got = _ready()
        if not got:
            raise unittest.SkipTest("Node 22, the graphics packages, Chrome or ffmpeg is not available")
        cls.real, cls.ffmpeg, cls.ffprobe = got
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.src = d / "src.mp4"
        subprocess.run([cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x203040:s=320x180:r=30:d=14",
                        "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=14", "-c:v", "libx264", "-g", "15",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.src)], check=True)
        data = dict(cls.real.as_dict())
        data["work_root"] = str(d / "work")
        data["edit"] = dict(data.get("edit") or {}, segment_pad=0.5, safe_framing=False)
        cls.settings = Settings(data, Path("settings.json"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def spec(self, text="LAND"):
        return {"name": "mot", "profile": "balanced",
                "output": {"preset": "youtube_1080p30", "width": 320, "height": 180, "fps": 30, "x264_preset": "ultrafast"},
                "sources": {"v": {"file": str(self.src)}},
                "clips": [{"id": "a", "src": "v", "in": 1.0, "out": 9.0, "overlays": [probe(5.0, hit=0.5, dur=1.0, props={"text": text})]},
                          {"id": "c", "type": "card", "dur": 2.0, "bg": "#0B0B0F", "transition_in": "cut", "overlays": [probe(0.5, hit=0.5, dur=1.0, props={"text": text})]}],
                "tracks": {"music": [], "sfx": []}}

    def amber(self, path, t):
        _, f = next(editscene.read_frames(self.ffmpeg, path, t, t + 0.1, 30.0, (320, 180)))
        r, g, b = (int(f[26, 60, i]) for i in range(3))              # inside the bar: x 17..117, y 17..37 of 320x180
        return r > 200 and 140 < g < 210 and b < 60

    def test_the_graphic_lands_on_the_hit_and_is_gone_afterwards_on_a_clip_and_on_a_card(self):
        report = stage.render_edit(self.spec(), self.settings)
        self.assertEqual(report["motion"], {"graphics": 1, "drawn": 1})        # one distinct graphic (both overlays share it)
        out = report["output"]
        self.assertFalse(self.amber(out, 3.0))
        self.assertTrue(self.amber(out, 4.0 + 0.1), "no bar at the hit (clip: 5.0 - 1.0 = 4.0 s)")
        self.assertFalse(self.amber(out, 6.0))
        card = 8.0
        self.assertTrue(self.amber(out, card + 0.5 + 0.1), "no bar on the card at its hit")
        self.assertFalse(self.amber(out, card + 1.8))

    def test_a_second_render_draws_nothing_and_an_edited_prop_draws_one(self):
        stage.render_edit(self.spec("AGAIN"), self.settings)
        again = stage.render_edit(self.spec("AGAIN"), self.settings)
        self.assertEqual(again["motion"]["drawn"], 0)
        s = self.spec("AGAIN")
        s["clips"][0]["overlays"][0]["props"] = {"text": "OTHER"}
        self.assertEqual(stage.render_edit(s, self.settings)["motion"]["drawn"], 1)


if __name__ == "__main__":
    unittest.main()
