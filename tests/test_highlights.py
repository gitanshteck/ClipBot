"""Pure z-score/span-merging math for the energy/chat notable-moment signals.

No network or real Claude/Kick calls - `compute_energy_spikes` runs against a
small synthetic WAV file, `compute_chat_spikes` against a synthetic chat.json
dict, and `notable_moments` against a real temp Workspace with neither
audio.wav nor chat.json present (graceful-degradation path).

Run: python -m unittest discover -s tests
"""

import struct
import tempfile
import unittest
import wave
from pathlib import Path

from clipbot import highlights as H
from clipbot.config import Settings
from clipbot.workspace import Workspace


def _write_wav(path, segments, rate=8000):
    """segments: [(duration_seconds, amplitude)]. Alternating +/-amplitude
    samples give each block an RMS equal to `amplitude`."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for duration, amplitude in segments:
            n = int(rate * duration)
            for i in range(n):
                val = amplitude if i % 2 == 0 else -amplitude
                frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))


class TestComputeEnergySpikes(unittest.TestCase):
    def test_flags_only_the_loud_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.wav"
            _write_wav(
                path,
                [(20.0, 100), (5.0, 20000), (20.0, 100)],
                rate=8000,
            )
            spikes = H.compute_energy_spikes(path, bin_seconds=1.0, z_threshold=2.0)
            self.assertEqual(len(spikes), 1)
            spike = spikes[0]
            # The burst runs [20, 25) - allow a bin of slack on each edge.
            self.assertLessEqual(spike["start"], 21.0)
            self.assertGreaterEqual(spike["start"], 19.0)
            self.assertLessEqual(spike["end"], 26.0)
            self.assertGreaterEqual(spike["end"], 24.0)
            self.assertGreater(spike["z_score"], 2.0)

    def test_flat_audio_flags_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.wav"
            _write_wav(path, [(10.0, 500)], rate=8000)
            spikes = H.compute_energy_spikes(path, bin_seconds=1.0, z_threshold=2.0)
            self.assertEqual(spikes, [])


class TestComputeChatSpikes(unittest.TestCase):
    def _doc(self, offsets):
        return {
            "status": "ok",
            "messages": [
                {"offset": t, "id": str(i), "user": {"name": "u"}, "text": "hi"}
                for i, t in enumerate(offsets)
            ],
        }

    def test_flags_the_burst_window(self):
        # Sparse baseline (one message every 10s) plus a dense burst at 50-55s.
        baseline = [float(t) for t in range(0, 100, 10)]
        burst = [50.0, 50.5, 51.0, 51.5, 52.0, 52.5, 53.0, 53.5]
        doc = self._doc(baseline + burst)
        spikes = H.compute_chat_spikes(
            doc, offset_seconds=0.0, duration=100.0, bin_seconds=5.0, z_threshold=2.0
        )
        self.assertTrue(spikes, "expected at least one flagged span")
        starts = [s["start"] for s in spikes]
        self.assertTrue(any(45.0 <= s <= 55.0 for s in starts))
        self.assertTrue(all(s["message_rate_multiplier"] > 1.0 for s in spikes))

    def test_applies_the_offset_correction(self):
        # A burst stored at raw offset 160-165 should land at VOD time 50-55
        # once the 110s chat_offset_seconds correction is subtracted - same
        # sign convention as stages.chat.messages_between.
        baseline = [float(t) + 110.0 for t in range(0, 100, 10)]
        burst = [160.0, 160.5, 161.0, 161.5, 162.0, 162.5, 163.0, 163.5]
        doc = self._doc(baseline + burst)
        spikes = H.compute_chat_spikes(
            doc, offset_seconds=110.0, duration=100.0, bin_seconds=5.0, z_threshold=2.0
        )
        self.assertTrue(any(45.0 <= s["start"] <= 55.0 for s in spikes))

    def test_empty_messages_flags_nothing(self):
        spikes = H.compute_chat_spikes(
            self._doc([]), offset_seconds=0.0, duration=100.0
        )
        self.assertEqual(spikes, [])


class TestNotableMoments(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws").ensure()
        self.settings = Settings({}, Path("."))

    def tearDown(self):
        self._tmp.cleanup()

    def test_degrades_gracefully_with_nothing_on_disk(self):
        # No audio.wav, no chat.json - both signals should be skipped, not
        # raise, and the result is an empty list.
        self.ws.update_state(duration=100.0)
        self.assertEqual(H.notable_moments(self.ws, self.settings), [])

    def test_disabled_returns_empty_even_with_data(self):
        settings = Settings({"analyze": {"signals": {"enabled": False}}}, Path("."))
        self.ws.update_state(duration=100.0)
        self.assertEqual(H.notable_moments(self.ws, settings), [])


if __name__ == "__main__":
    unittest.main()
