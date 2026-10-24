"""PANNs `Cnn14_16k`: a pretrained audio tagger (527 AudioSet classes: Speech, Laughter, Shout, Gunshot, Music...).

The architecture is the one in qiuqiangkong/audioset_tagging_cnn (`pytorch/models.py`, MIT), written out here rather
than installed: the model is six conv blocks and two linear layers, and installing the pip package would run someone
else's setup code to save about eighty lines. Only the *weights* are third-party - `Cnn14_16k_mAP=0.438.pth`
(PANNs authors' release, Zenodo record 3987831, CC BY 4.0, ~359 MB), downloaded by an explicit command, pinned to a
checksum, and loaded with `torch.load(weights_only=True)`, which refuses a pickle that carries anything but tensors.
The file is torch's legacy format and also holds a numpy array of training statistics, so `load_tensors_only` admits
exactly the six globals its pickle names (read with `pickletools`, which executes nothing) and nothing else.

The 16 kHz variant, not the 32 kHz one: `audio.wav` is 16 kHz mono, so there is no resampling and no band the model
expects that the file does not have (and it scores a little higher, mAP 0.438 against 0.431).

**Dense tagging.** The network ends in a global max+mean pool over time, so one forward pass gives one answer for a
whole window. To tag every second of a multi-hour stream, `tag_second_grid` runs the convolutional trunk once over
each minute of audio (with a margin either side so the borders see context) and pools the trunk's frames per 2 s
window around each second before the two linear layers - the same pooling the model does, applied per window, at one
trunk pass instead of one per window. The test compares it with real per-window inference.

Everything that needs torch imports it inside the function: the rest of ClipBot does not require it.
"""

import hashlib
import json
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import Settings
from .utils import StageError, get_logger

log = get_logger(__name__)

SAMPLE_RATE = 16000
N_FFT = 512
HOP = 160                 # 10 ms
MELS = 64
FMIN, FMAX = 50.0, 8000.0
CLASSES = 527
FRAME_SECONDS = 0.32      # one trunk frame (five 2x pools of a 10 ms mel frame)
WINDOW_SECONDS = 2.0      # a second is tagged from the 2 s around it
CHUNK_SECONDS = 60.0
MARGIN_SECONDS = 2.0

CHECKPOINT_NAME = "Cnn14_16k_mAP=0.438.pth"
CHECKPOINT_URL = "https://zenodo.org/record/3987831/files/Cnn14_16k_mAP%3D0.438.pth?download=1"
CHECKPOINT_MD5 = "362fc5ff18f1d6ad2f6d464b45893f2c"       # the record's own listing
ALLOWED_HOSTS = ("zenodo.org",)
LABELS_FILE = Path(__file__).resolve().parent / "data" / "audioset_labels.csv"

# Category -> AudioSet class indices (class_labels_indices.csv). A category's score for a second is the highest
# probability among its classes.
CATEGORIES: Dict[str, Tuple[int, ...]] = {
    "speech": (0,),                      # Speech
    "laughter": (16, 18, 20, 21),        # Laughter, Giggle, Belly laugh, Chuckle (not Baby laughter)
    "shout": (8, 11, 14),                # Shout, Yell, Screaming
    "gunfire": (427, 428, 429),          # Gunshot/gunfire, Machine gun, Fusillade
    "explosion": (426, 430),             # Explosion, Artillery fire
    "cheer": (66, 67),                   # Cheering, Applause
    "music": (137,),                     # Music
    "silence": (500,),                   # Silence
}


# --------------------------------------------------------------------------
# the checkpoint: where it lives, whether it is the file we pinned, how to fetch it
# --------------------------------------------------------------------------


def checkpoint_path(settings: Settings) -> Path:
    configured = settings.get("events.model_file", None)
    if configured:
        return Path(str(configured))
    return Path(settings.work_root) / "_cache" / "models" / "panns" / CHECKPOINT_NAME


def _md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_checkpoint(path: Path) -> bool:
    """True when `path` is the pinned file. Hashing 359 MB takes seconds, so a sidecar remembers a pass for that
    exact size and mtime."""
    path = Path(path)
    if not path.is_file():
        return False
    st = path.stat()
    side = path.with_name(path.name + ".verified")
    try:
        rec = json.loads(side.read_text(encoding="utf-8"))
        if rec.get("md5") == CHECKPOINT_MD5 and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns:
            return True
    except (OSError, ValueError):
        pass
    if _md5(path) != CHECKPOINT_MD5:
        return False
    side.write_text(json.dumps({"md5": CHECKPOINT_MD5, "size": st.st_size, "mtime_ns": st.st_mtime_ns}), encoding="utf-8")
    return True


def fetch_checkpoint(settings: Settings, progress: Optional[Callable[[int, Optional[int]], None]] = None,
                     opener: Optional[Callable[..., Any]] = None) -> Path:
    """Download the pinned checkpoint to `checkpoint_path`. Only ever run by an explicit command. The host must be
    on `ALLOWED_HOSTS` (the final URL after redirects too), the file is written as `.part`, and it only takes its
    name once its MD5 equals the record's - a truncated or substituted file is deleted, not kept."""
    dest = checkpoint_path(settings)
    if verify_checkpoint(dest):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    host = urllib.parse.urlparse(CHECKPOINT_URL).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise StageError("refusing to fetch from {0}".format(host))
    part = dest.with_name(dest.name + ".part")
    open_url = opener or urllib.request.urlopen
    h = hashlib.md5()
    done = 0
    try:
        with open_url(CHECKPOINT_URL, timeout=60) as resp, part.open("wb") as out:
            final = urllib.parse.urlparse(resp.geturl()).hostname or ""
            if final not in ALLOWED_HOSTS and not any(final.endswith("." + a) for a in ALLOWED_HOSTS):
                raise StageError("the download redirected to {0}, which is not on the allow list".format(final))
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            for block in iter(lambda: resp.read(1 << 20), b""):
                out.write(block)
                h.update(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if h.hexdigest() != CHECKPOINT_MD5:
            raise StageError("the downloaded file's MD5 is {0}, not the record's {1}; deleted".format(h.hexdigest(), CHECKPOINT_MD5))
        part.replace(dest)
    except Exception:
        if part.exists():
            part.unlink()
        raise
    verify_checkpoint(dest)
    return dest


def labels() -> List[str]:
    """The 527 AudioSet display names, in the model's output order."""
    out: List[str] = []
    for line in LABELS_FILE.read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip():
            out.append(line.split(",", 2)[2].strip().strip('"'))
    return out


# --------------------------------------------------------------------------
# the model (torch, imported lazily)
# --------------------------------------------------------------------------


def _torch() -> Any:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise StageError("audio events need PyTorch (pip install -r requirements-events.txt): {0}".format(exc))
    return torch


def build_model() -> Any:
    """`Cnn14_16k` with its weights uninitialised. Eval mode is set by `load_model`."""
    torch = _torch()
    nn = torch.nn
    F = torch.nn.functional

    class LogMel(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.register_buffer("melW", torch.zeros(N_FFT // 2 + 1, MELS))

        def forward(self, power: Any) -> Any:                      # (B, T, bins) -> (B, 1, T, mels)
            mel = torch.matmul(power, self.melW)
            return (10.0 * torch.log10(torch.clamp(mel, min=1e-10))).unsqueeze(1)

    class ConvBlock(nn.Module):
        def __init__(self, cin: int, cout: int) -> None:
            super().__init__()
            self.conv1 = nn.Conv2d(cin, cout, kernel_size=3, stride=1, padding=1, bias=False)
            self.conv2 = nn.Conv2d(cout, cout, kernel_size=3, stride=1, padding=1, bias=False)
            self.bn1 = nn.BatchNorm2d(cout)
            self.bn2 = nn.BatchNorm2d(cout)

        def forward(self, x: Any, pool: Tuple[int, int]) -> Any:
            x = F.relu(self.bn1(self.conv1(x)))
            x = F.relu(self.bn2(self.conv2(x)))
            return F.avg_pool2d(x, kernel_size=pool)

    class Cnn14_16k(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.logmel_extractor = LogMel()
            self.bn0 = nn.BatchNorm2d(MELS)
            self.conv_block1 = ConvBlock(1, 64)
            self.conv_block2 = ConvBlock(64, 128)
            self.conv_block3 = ConvBlock(128, 256)
            self.conv_block4 = ConvBlock(256, 512)
            self.conv_block5 = ConvBlock(512, 1024)
            self.conv_block6 = ConvBlock(1024, 2048)
            self.fc1 = nn.Linear(2048, 2048, bias=True)
            self.fc_audioset = nn.Linear(2048, CLASSES, bias=True)
            self.register_buffer("window", torch.hann_window(N_FFT, periodic=True), persistent=False)

        def features(self, wav: Any) -> Any:
            """(B, samples) -> trunk frames (B, T', 2048), one per `FRAME_SECONDS`."""
            spec = torch.stft(wav, n_fft=N_FFT, hop_length=HOP, win_length=N_FFT, window=self.window, center=True,
                              pad_mode="reflect", return_complex=True)
            power = (spec.real ** 2 + spec.imag ** 2).transpose(1, 2)          # (B, T, bins)
            x = self.logmel_extractor(power)
            x = x.transpose(1, 3)
            x = self.bn0(x)
            x = x.transpose(1, 3)
            for block in (self.conv_block1, self.conv_block2, self.conv_block3, self.conv_block4, self.conv_block5):
                x = block(x, (2, 2))
            x = self.conv_block6(x, (1, 1))
            return torch.mean(x, dim=3).transpose(1, 2)                         # (B, T', 2048)

        def head(self, pooled: Any) -> Any:
            """Pooled trunk features (N, 2048) -> class probabilities (N, 527)."""
            x = F.relu(self.fc1(pooled))
            return torch.sigmoid(self.fc_audioset(x))

        def forward(self, wav: Any) -> Any:
            f = self.features(wav)
            return self.head(f.max(dim=1).values + f.mean(dim=1))

    return Cnn14_16k()


def _safe_globals() -> List[Any]:
    """The few non-tensor globals the PANNs checkpoint's pickle refers to, found by disassembling it (nothing was
    executed): `OrderedDict`, torch's tensor rebuild and storages - all admitted by torch already - and, for the one
    numpy array stored beside the weights (`indexes_per_class`, 937,432 int64 indices left over from the training
    sampler), `numpy.ndarray`, `numpy.dtype`, the int64 dtype class, numpy's array `_reconstruct` and
    `_codecs.encode` (how an old pickle spells the array's bytes). Nothing else may be unpickled."""
    import _codecs

    multiarray = np._core.multiarray if hasattr(np, "_core") else np.core.multiarray       # numpy 2 / numpy 1
    # the checkpoint spells it numpy.core...; an array saved by numpy 2 spells it numpy._core... - one function
    return [(multiarray._reconstruct, "numpy.core.multiarray._reconstruct"), (multiarray._reconstruct, "numpy._core.multiarray._reconstruct"),
            np.ndarray, np.dtype, type(np.dtype("i8")), _codecs.encode]


def load_tensors_only(path: Path) -> Any:
    """`torch.load` with `weights_only=True` and the allow-list above: a checkpoint that carries any other object -
    code in a pickle - is refused, not run."""
    torch = _torch()
    with torch.serialization.safe_globals(_safe_globals()):
        return torch.load(str(path), map_location="cpu", weights_only=True)


def load_model(path: Path) -> Any:
    """The model with the pinned checkpoint loaded, in eval mode. Refuses a file that is not the pinned one."""
    path = Path(path)
    if not path.is_file():
        raise StageError("the PANNs checkpoint is not at {0}; fetch it with: clipbot events fetch-model".format(path))
    if not verify_checkpoint(path):
        raise StageError("{0} is not the pinned checkpoint (MD5 {1}); delete it and run: clipbot events fetch-model".format(path, CHECKPOINT_MD5))
    ckpt = load_tensors_only(path)
    state = ckpt["model"] if isinstance(ckpt, dict) and "model" in ckpt else ckpt
    state = {k: v for k, v in state.items() if not k.startswith("spectrogram_extractor.")}   # torch.stft stands in for it
    model = build_model()
    missing, unexpected = model.load_state_dict(state, strict=False)
    missing = [k for k in missing if k != "window"]
    if missing or unexpected:
        raise StageError("the checkpoint does not match the model (missing {0}, unexpected {1})".format(missing[:4], unexpected[:4]))
    model.eval()
    return model


# --------------------------------------------------------------------------
# tagging
# --------------------------------------------------------------------------


def tag_window(model: Any, wav: np.ndarray) -> np.ndarray:
    """Class probabilities (527,) for one waveform, exactly as the model was trained to be used."""
    torch = _torch()
    with torch.no_grad():
        return model(torch.from_numpy(np.ascontiguousarray(wav, dtype=np.float32))[None]).numpy()[0]


def window_frames(second: int, n_frames: int, origin: float = 0.0, window: float = WINDOW_SECONDS) -> Tuple[int, int]:
    """Trunk frames [a, b) whose centres lie in the `window` seconds centred on the second `[second, second+1)`;
    `origin` is the time of frame 0. At least one frame."""
    lo = second + 0.5 - window / 2.0 - origin
    hi = second + 0.5 + window / 2.0 - origin
    a = int(np.ceil((lo - FRAME_SECONDS / 2.0) / FRAME_SECONDS))
    b = int(np.ceil((hi - FRAME_SECONDS / 2.0) / FRAME_SECONDS))
    a = min(max(a, 0), max(n_frames - 1, 0))
    b = min(max(b, a + 1), n_frames)
    return a, b


def tag_chunk(model: Any, wav: np.ndarray, seconds: Sequence[int], origin: float) -> np.ndarray:
    """Probabilities (len(seconds), 527) for the given seconds of one chunk of audio whose first sample is at
    `origin` seconds on the stream's clock."""
    torch = _torch()
    with torch.no_grad():
        f = model.features(torch.from_numpy(np.ascontiguousarray(wav, dtype=np.float32))[None])[0]     # (T', 2048)
        n = f.shape[0]
        pooled = []
        for s in seconds:
            a, b = window_frames(int(s), n, origin)
            seg = f[a:b]
            pooled.append(seg.max(dim=0).values + seg.mean(dim=0))
        return model.head(torch.stack(pooled)).numpy()


def read_wav(path: Path, start: float, end: float) -> np.ndarray:
    """Mono float32 samples of [start, end) seconds of a 16 kHz 16-bit wav (zero-padded past its end)."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise StageError("{0} is not 16 kHz mono 16-bit (it is {1} Hz, {2} ch, {3}-bit)".format(
                path, w.getframerate(), w.getnchannels(), w.getsampwidth() * 8))
        a = max(0, int(round(start * SAMPLE_RATE)))
        b = int(round(end * SAMPLE_RATE))
        w.setpos(min(a, w.getnframes()))
        raw = w.readframes(max(0, min(b, w.getnframes()) - a))
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    want = b - int(round(max(start, 0.0) * SAMPLE_RATE))
    if len(x) < want:
        x = np.concatenate([x, np.zeros(want - len(x), dtype=np.float32)])
    return x


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def tag_second_grid(model: Any, wav_path: Path, duration: Optional[float] = None, first: int = 0, last: Optional[int] = None,
                    classes: Optional[Sequence[int]] = None,
                    progress: Optional[Callable[[int, int], None]] = None,
                    cancelled: Optional[Callable[[], bool]] = None) -> np.ndarray:
    """Probabilities for every second from `first` to `last` (exclusive; default the whole file): shape (seconds,
    len(classes) or 527). Minute by minute, each with `MARGIN_SECONDS` of context either side."""
    dur = duration if duration is not None else wav_seconds(wav_path)
    n_sec = int(np.ceil(dur))
    last = n_sec if last is None else min(last, n_sec)
    cols = list(classes) if classes is not None else None
    out = np.zeros((max(0, last - first), len(cols) if cols is not None else CLASSES), dtype=np.float32)
    s = first
    while s < last:
        if cancelled and cancelled():
            raise StageError("cancelled")
        e = min(last, s + int(CHUNK_SECONDS))
        a = max(0.0, s - MARGIN_SECONDS)
        b = e + MARGIN_SECONDS
        wav = read_wav(wav_path, a, b)
        if len(wav) < N_FFT:
            s = e
            continue
        p = tag_chunk(model, wav, list(range(s, e)), a)
        out[s - first:e - first] = p[:, cols] if cols is not None else p
        s = e
        if progress:
            progress(s - first, last - first)
    return out
