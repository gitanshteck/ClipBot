"""panns.py: the checkpoint is the pinned file or it is not used; the label table matches the model's classes;
the frame arithmetic; the architecture's shapes. The trained weights are never needed here."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from clipbot import panns
from clipbot.config import Settings
from clipbot.utils import StageError

try:
    import torch  # noqa: F401
    HAVE_TORCH = True
except ImportError:  # pragma: no cover
    HAVE_TORCH = False


def settings(tmp, **events):
    return Settings({"work_root": tmp, "events": events}, Path("s.json"))


class TestLabels(unittest.TestCase):
    def test_there_are_527_labels_in_the_models_order(self):
        names = panns.labels()
        self.assertEqual(len(names), panns.CLASSES)
        self.assertEqual(names[0], "Speech")

    def test_every_category_class_is_what_its_name_says(self):
        names = panns.labels()
        expected = {"speech": ["Speech"], "laughter": ["Laughter", "Giggle", "Belly laugh", "Chuckle, chortle"],
                    "shout": ["Shout", "Yell", "Screaming"], "gunfire": ["Gunshot, gunfire", "Machine gun", "Fusillade"],
                    "explosion": ["Explosion", "Artillery fire"], "cheer": ["Cheering", "Applause"], "music": ["Music"],
                    "silence": ["Silence"]}
        self.assertEqual(set(expected), set(panns.CATEGORIES))
        for cat, want in expected.items():
            self.assertEqual([names[i] for i in panns.CATEGORIES[cat]], want, cat)


class TestWindowFrames(unittest.TestCase):
    def test_a_second_takes_the_frames_centred_in_the_two_seconds_around_it(self):
        # frame j is centred at (j + 0.5) * 0.32 s; the window for second 10 is [9.5, 11.5)
        a, b = panns.window_frames(10, 1000)
        centres = [(j + 0.5) * panns.FRAME_SECONDS for j in range(a, b)]
        self.assertTrue(all(9.5 <= c < 11.5 for c in centres))
        self.assertTrue((a - 1 + 0.5) * panns.FRAME_SECONDS < 9.5 and (b + 0.5) * panns.FRAME_SECONDS >= 11.5)
        self.assertGreaterEqual(b - a, 5)

    def test_the_origin_shifts_the_frames_not_their_number(self):
        a1, b1 = panns.window_frames(70, 300, origin=60.0)
        a2, b2 = panns.window_frames(10, 300, origin=0.0)
        self.assertEqual((a1, b1), (a2, b2))

    def test_the_edges_never_give_an_empty_or_out_of_range_window(self):
        for s, n in ((0, 20), (19, 20), (50, 20), (-3, 20), (0, 1)):
            a, b = panns.window_frames(s, n)
            self.assertTrue(0 <= a < b <= max(n, 1), (s, n, a, b))


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def pinned(self, data=b"weights"):
        """Pretend the pinned MD5 is that of `data` for the duration of a test."""
        return mock.patch.object(panns, "CHECKPOINT_MD5", hashlib.md5(data).hexdigest())

    def test_the_default_location_is_under_the_work_cache_and_can_be_overridden(self):
        self.assertEqual(panns.checkpoint_path(settings(self.tmp)), Path(self.tmp) / "_cache" / "models" / "panns" / panns.CHECKPOINT_NAME)
        self.assertEqual(panns.checkpoint_path(settings(self.tmp, model_file="D:/m/x.pth")), Path("D:/m/x.pth"))

    def test_only_the_pinned_file_verifies_and_a_pass_is_remembered_for_that_exact_file(self):
        p = Path(self.tmp) / "m.pth"
        p.write_bytes(b"weights")
        with self.pinned():
            self.assertTrue(panns.verify_checkpoint(p))
            self.assertTrue(p.with_name("m.pth.verified").exists())
            with mock.patch.object(panns, "_md5", side_effect=AssertionError("hashed again")):
                self.assertTrue(panns.verify_checkpoint(p))              # the sidecar answered
            p.write_bytes(b"weightz")                                    # changed: the memory no longer applies
            self.assertFalse(panns.verify_checkpoint(p))
        self.assertFalse(panns.verify_checkpoint(Path(self.tmp) / "absent.pth"))

    def test_a_download_is_kept_only_when_its_md5_matches_and_its_host_is_allowed(self):
        class Resp(io.BytesIO):
            headers = {"Content-Length": "7"}

            def __init__(self, data, url):
                super().__init__(data)
                self.url = url

            def geturl(self):
                return self.url

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        s = settings(self.tmp)
        dest = panns.checkpoint_path(s)
        with self.pinned():
            got = panns.fetch_checkpoint(s, opener=lambda url, timeout=0: Resp(b"weights", "https://zenodo.org/records/1/files/x"))
            self.assertEqual(got, dest)
            self.assertEqual(dest.read_bytes(), b"weights")
            dest.unlink()
            dest.with_name(dest.name + ".verified").unlink()
            with self.assertRaises(StageError):                                           # wrong bytes: deleted, not kept
                panns.fetch_checkpoint(s, opener=lambda url, timeout=0: Resp(b"tampered", "https://zenodo.org/x"))
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_name(dest.name + ".part").exists())
            with self.assertRaises(StageError):                                           # redirected somewhere else
                panns.fetch_checkpoint(s, opener=lambda url, timeout=0: Resp(b"weights", "https://evil.example/x"))
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_a_present_and_verified_checkpoint_is_not_downloaded_again(self):
        s = settings(self.tmp)
        dest = panns.checkpoint_path(s)
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"weights")
        with self.pinned():
            panns.fetch_checkpoint(s, opener=lambda *a, **k: self.fail("downloaded again"))

    def test_the_pinned_source_is_the_record_the_plan_names(self):
        self.assertIn("zenodo.org/record/3987831/files/Cnn14_16k", panns.CHECKPOINT_URL)
        self.assertEqual(panns.CHECKPOINT_MD5, "362fc5ff18f1d6ad2f6d464b45893f2c")
        self.assertEqual(panns.ALLOWED_HOSTS, ("zenodo.org",))


@unittest.skipUnless(HAVE_TORCH, "PyTorch is not installed")
class TestModel(unittest.TestCase):
    def test_loading_refuses_a_file_that_is_not_the_pinned_one(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.pth"
            p.write_bytes(b"not the checkpoint")
            with self.assertRaises(StageError) as ctx:
                panns.load_model(p)
        self.assertIn("pinned", str(ctx.exception))

    def test_loading_refuses_a_missing_file_and_says_how_to_fetch_it(self):
        with self.assertRaises(StageError) as ctx:
            panns.load_model(Path("D:/nowhere/m.pth"))
        self.assertIn("fetch-model", str(ctx.exception))

    def test_the_checkpoint_is_loaded_tensors_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.pth"
            p.write_bytes(b"x")
            calls = []

            def fake_load(path, **kw):
                calls.append(kw)
                raise RuntimeError("stop here")

            with mock.patch.object(panns, "verify_checkpoint", return_value=True), \
                    mock.patch("torch.load", side_effect=fake_load), self.assertRaises(RuntimeError):
                panns.load_model(p)
        self.assertIs(calls[0].get("weights_only"), True)

    def test_a_pickle_that_carries_code_is_refused_by_the_safe_load(self):
        import torch

        ran = []

        class Evil:
            def __reduce__(self):
                return (ran.append, ("this must never run",))

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "evil.pth"
            torch.save({"model": Evil()}, str(p))
            with self.assertRaises(Exception):
                panns.load_tensors_only(p)
        self.assertEqual(ran, [])

    def test_the_numpy_array_the_real_checkpoint_carries_is_admitted_and_nothing_else_numpy_does(self):
        import torch

        class Obj:
            def __reduce__(self):
                return (np.array, ([1, 2, 3],))                             # a numpy *callable* the allow-list does not name

        with tempfile.TemporaryDirectory() as d:
            ok = Path(d) / "ok.pth"
            torch.save({"iteration": 3, "model": {"w": torch.zeros(2)}, "sampler": {"indexes_per_class": [np.arange(5, dtype="int64")]}}, str(ok))
            got = panns.load_tensors_only(ok)
            self.assertEqual(got["iteration"], 3)
            self.assertEqual(list(got["sampler"]["indexes_per_class"][0]), [0, 1, 2, 3, 4])
            bad = Path(d) / "bad.pth"
            torch.save({"model": Obj()}, str(bad))
            with self.assertRaises(Exception):
                panns.load_tensors_only(bad)

    def test_the_architecture_has_the_published_parameter_names_and_shapes(self):
        model = panns.build_model()
        sd = model.state_dict()
        self.assertEqual(tuple(sd["fc_audioset.weight"].shape), (527, 2048))
        self.assertEqual(tuple(sd["conv_block1.conv1.weight"].shape), (64, 1, 3, 3))
        self.assertEqual(tuple(sd["conv_block6.conv2.weight"].shape), (2048, 2048, 3, 3))
        self.assertEqual(tuple(sd["bn0.weight"].shape), (64,))
        self.assertEqual(tuple(sd["logmel_extractor.melW"].shape), (257, 64))
        self.assertNotIn("window", sd)

    def test_the_mel_filterbank_is_the_checkpoints_matrix_and_the_log_is_a_power_in_decibels(self):
        import torch

        model = panns.build_model().eval()
        mel_w = torch.zeros(257, 64)
        mel_w[3, 0] = 1.0                                        # mel bin 0 = fft bin 3
        mel_w[10, 1] = 0.5
        with torch.no_grad():
            model.logmel_extractor.melW.copy_(mel_w)
            power = torch.zeros(1, 4, 257)
            power[0, :, 3] = 100.0
            power[0, :, 10] = 8.0
            out = model.logmel_extractor(power)
        self.assertEqual(tuple(out.shape), (1, 1, 4, 64))
        self.assertAlmostEqual(float(out[0, 0, 0, 0]), 20.0, places=3)           # 10 log10(100)
        self.assertAlmostEqual(float(out[0, 0, 0, 1]), 10 * np.log10(4.0), places=3)
        self.assertAlmostEqual(float(out[0, 0, 0, 5]), -100.0, places=3)         # an empty bin is clamped at 1e-10

    def test_each_chunk_is_read_with_context_either_side_so_its_borders_see_the_audio_around_them(self):
        asked = []

        def fake_read(path, a, b):
            asked.append((a, b))
            return np.zeros(int((b - a) * 16000), dtype=np.float32)

        with mock.patch.object(panns, "read_wav", side_effect=fake_read), mock.patch.object(panns, "CHUNK_SECONDS", 10.0),                 mock.patch.object(panns, "tag_chunk", side_effect=lambda m, w, secs, origin: np.zeros((len(secs), panns.CLASSES), dtype=np.float32)):
            panns.tag_second_grid(object(), Path("x.wav"), duration=25.0)
        self.assertEqual(asked, [(0.0, 12.0), (8.0, 22.0), (18.0, 27.0)])         # 10 s chunks + 2 s margin, clamped at the start

    def test_one_second_of_audio_gives_trunk_frames_at_the_documented_rate(self):
        import torch

        model = panns.build_model().eval()
        with torch.no_grad():
            f = model.features(torch.zeros(1, 16000 * 4))
        self.assertEqual(f.shape[0], 1)
        self.assertEqual(f.shape[2], 2048)
        self.assertAlmostEqual(f.shape[1] * panns.FRAME_SECONDS, 4.0, delta=0.7)

    def test_tag_chunk_returns_one_probability_row_per_second_asked_for(self):
        model = panns.build_model().eval()
        wav = (np.random.RandomState(0).randn(16000 * 8) * 0.01).astype(np.float32)
        p = panns.tag_chunk(model, wav, [2, 3, 4], origin=0.0)
        self.assertEqual(p.shape, (3, panns.CLASSES))
        self.assertTrue(((p >= 0) & (p <= 1)).all())

    def wav_file(self, d, seconds):
        import wave

        p = Path(d) / "a.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.random.RandomState(1).randn(seconds * 16000) * 300).astype("<i2").tobytes())
        return p

    def test_the_second_grid_covers_every_second_across_chunk_boundaries_and_can_be_narrowed_to_some_classes(self):
        model = panns.build_model().eval()
        seen = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(panns, "CHUNK_SECONDS", 8.0):
            p = self.wav_file(d, 21)
            full = panns.tag_second_grid(model, p, progress=lambda done, total: seen.append((done, total)))
            part = panns.tag_second_grid(model, p, first=5, last=15, classes=[0, 16, 427])
        self.assertEqual(full.shape, (21, panns.CLASSES))
        self.assertEqual(part.shape, (10, 3))
        self.assertEqual(seen[-1], (21, 21))
        self.assertEqual(len(seen), 3)                                      # 8 + 8 + 5 seconds
        self.assertTrue(((full >= 0) & (full <= 1)).all())
        self.assertTrue(np.isfinite(full).all())

    def test_cancelling_between_chunks_stops_the_scan(self):
        model = panns.build_model().eval()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(panns, "CHUNK_SECONDS", 8.0):
            with self.assertRaises(StageError):
                panns.tag_second_grid(model, self.wav_file(d, 21), cancelled=lambda: True)


class TestReadWav(unittest.TestCase):
    def write(self, d, seconds=3, rate=16000, channels=1):
        import wave

        p = Path(d) / "a.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.arange(seconds * rate * channels) % 1000).astype("<i2").tobytes())
        return p

    def test_a_range_is_read_and_padded_with_silence_past_the_end(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, seconds=3)
            self.assertEqual(len(panns.read_wav(p, 1.0, 2.0)), 16000)
            x = panns.read_wav(p, 2.5, 4.0)
            self.assertEqual(len(x), 24000)
            self.assertTrue((x[8000:] == 0).all())
            self.assertAlmostEqual(panns.wav_seconds(p), 3.0)

    def test_a_wav_that_is_not_16k_mono_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(StageError):
                panns.read_wav(self.write(d, rate=44100), 0, 1)
            with self.assertRaises(StageError):
                panns.read_wav(self.write(d, channels=2), 0, 1)


if __name__ == "__main__":
    unittest.main()
