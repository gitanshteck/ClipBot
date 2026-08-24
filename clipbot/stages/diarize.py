"""Opt-in stage: pyannote-based speaker diarization.

Deliberately not numbered into the main pipeline table and never part of
`clipbot run` or the dashboard's `pipeline` job kind - this machine has no
CUDA (the same limitation `transcribe.py` already documents for Whisper), so
a multi-hour VOD's diarization pass runs CPU-only and can take a while, and
it needs a Hugging Face access token most users won't have configured by
default. Requires `pip install -r requirements-diarize.txt` (not part of
`requirements.txt`, since it pulls in PyTorch).

Produces `diarization.json`: raw "who spoke when" turns with generic cluster
labels (`SPEAKER_00`, `SPEAKER_01`, ...) - pyannote clusters voices, it
doesn't know names. Naming a cluster or merging it into an existing named
speaker profile happens afterward, via `clipbot/speakers.py`'s
`rename_speaker` and `clipbot/library.py`'s `save_speaker`, never here.

diarization.json:
    {
      "model": "pyannote/speaker-diarization-3.1",
      "device": "cpu",
      "duration": 16492.07,
      "diarize_seconds": 812.4,
      "turn_count": 214,
      "speaker_count": 3,
      "turns": [{"start": 12.0, "end": 18.4, "speaker": "SPEAKER_00"}]
    }
"""

import contextlib
import os
import time
from pathlib import Path
from typing import Any, Dict

from ..config import Settings
from ..progress import NULL_PROGRESS, Progress
from ..utils import StageError, get_logger
from ..workspace import Workspace

log = get_logger(__name__)

STAGE = "diarize"

HF_TOKEN_HINT = (
    "1. Create a free account at https://huggingface.co\n"
    "2. Accept the user agreement at BOTH of these - the pipeline depends on "
    "the segmentation model as a separate gated model, and accepting only "
    "the first one is the single most common reason this fails:\n"
    "   - https://huggingface.co/pyannote/speaker-diarization-3.1\n"
    "   - https://huggingface.co/pyannote/segmentation-3.0\n"
    "3. Create an access token (read access is enough) at "
    "https://huggingface.co/settings/tokens\n"
    '4. Set it:  PowerShell:  $env:HF_TOKEN = "hf_..."'
)


@contextlib.contextmanager
def _trust_pyannote_checkpoints():
    """PyTorch 2.6 changed `torch.load`'s default to `weights_only=True`,
    which breaks pyannote's checkpoint loading: its pipeline/model
    checkpoints store plain Python objects alongside tensors (measured -
    `torch.torch_version.TorchVersion`, recording which torch version wrote
    the file), and the safe-unpickler used by `weights_only=True` doesn't
    know that class, so loading fails with `UnpicklingError`.

    pyannote's own `pl_load` helper always explicitly passes
    `weights_only=weights_only` (defaulting to `None` at its call site), so
    a `functools.partial` preset default would just get overridden right
    back by that explicit keyword - this instead wraps `torch.load` to force
    `weights_only=False` regardless of what the caller passes, for every
    nested call (pipeline config, segmentation model, embedding model) that
    happens during `Pipeline.from_pretrained`.

    Deliberately scoped to just that call, not a global process-wide
    setting: this is torch's own documented option (1) for a trusted
    source (see the error message), and the only checkpoints loaded here
    are the official ones this stage just downloaded from Hugging Face's
    pyannote org, not arbitrary user-supplied files.
    """
    import torch

    original_load = torch.load

    def _patched_load(*args, **kwargs):
        kwargs["weights_only"] = False
        return original_load(*args, **kwargs)

    torch.load = _patched_load
    try:
        yield
    finally:
        torch.load = original_load


def _resolve_device(requested: str) -> str:
    """Same shape as transcribe.py's _resolve_device - no ROCm backend, so a
    Radeon GPU falls through to CPU here too."""
    if requested and requested != "auto":
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception as exc:  # pragma: no cover - depends on local install
        log.debug("CUDA probe failed: %s", exc)
    return "cpu"


def _load_pipeline(settings: Settings):
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise StageError(
            "pyannote.audio is not installed.\n"
            "Install with: pip install -r requirements-diarize.txt"
        ) from exc

    token_env = str(settings.get("diarize.hf_token_env", "HF_TOKEN"))
    token = os.environ.get(token_env)
    if not token:
        raise StageError(
            "{0} is not set - pyannote needs a Hugging Face access token to "
            "download the pretrained model.\n{1}".format(token_env, HF_TOKEN_HINT)
        )

    model_name = str(settings.get("diarize.model", "pyannote/speaker-diarization-3.1"))
    device = _resolve_device(str(settings.get("diarize.device", "auto")))
    if device == "cpu":
        log.warning(
            "Running %s on CPU - this machine has no usable diarization GPU "
            "(same limitation as transcribe.py). Expect this to take a while "
            "on a long VOD.",
            model_name,
        )

    log.info("Loading %s (device=%s)", model_name, device)
    started = time.time()
    try:
        with _trust_pyannote_checkpoints():
            pipeline = Pipeline.from_pretrained(model_name, use_auth_token=token)
    except AttributeError as exc:
        # pyannote's own from_pretrained doesn't raise when a *sub*-model
        # (segmentation, embedding) is gated/inaccessible either - deep
        # inside its own construction code it logs a hint and returns None
        # from that sub-model's loader, then unconditionally calls
        # `.eval()` on it with no None-check at all. Surfaces here as
        # "AttributeError: 'NoneType' object has no attribute 'eval'",
        # which names neither the missing model nor what to do about it.
        raise StageError(
            "Could not fully load {0}: {1}\n"
            "This is almost always a sub-model's gated-model agreement not "
            "being accepted yet - accepting the top-level pipeline's terms "
            "is not enough on its own.\n{2}"
            .format(model_name, exc, HF_TOKEN_HINT)
        ) from exc
    if pipeline is None:
        # pyannote does not raise on a bad/gated/unauthorized token for the
        # *top-level* pipeline either - it logs a lengthy hint to the
        # console and returns None. Left unchecked, that turns into
        # "TypeError: 'NoneType' object is not callable" three lines later,
        # which points nowhere near the real cause.
        raise StageError(
            "Could not load {0}. This usually means either {1} is invalid, "
            "or you have not accepted the model's user agreement yet.\n{2}"
            .format(model_name, token_env, HF_TOKEN_HINT)
        )
    if device == "cuda":
        import torch

        pipeline.to(torch.device("cuda"))
    log.info("Pipeline ready in %.1fs", time.time() - started)
    return pipeline, model_name, device


def diarize_audio(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Run speaker diarization on the workspace audio. Returns diarization.json's path."""
    target = ws.diarization_path
    if target.exists() and not force:
        log.info("Diarization already exists, skipping: %s", target.name)
        return target

    if not ws.audio_path.exists():
        raise StageError("No audio at {0}. Run the audio stage first.".format(ws.audio_path))

    pipeline, model_name, device = _load_pipeline(settings)

    kwargs: Dict[str, Any] = {}
    for key in ("num_speakers", "min_speakers", "max_speakers"):
        value = settings.get("diarize." + key)
        if value:
            kwargs[key] = int(value)

    log.info(
        "Diarizing %s - a single blocking pass, no per-segment progress or "
        "mid-run cancellation the way transcribe.py's lazy generator allows",
        ws.audio_path.name,
    )
    progress.phase("diarize")
    progress.check_cancelled()
    started = time.time()
    diarization = pipeline(str(ws.audio_path), **kwargs)
    elapsed = time.time() - started

    turns = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        turns.append({
            "start": round(float(turn.start), 3),
            "end": round(float(turn.end), 3),
            "speaker": str(speaker),
        })
    turns.sort(key=lambda t: t["start"])
    speaker_count = len(set(t["speaker"] for t in turns))

    duration = float(ws.read_state().get("duration") or 0.0)
    speed = (duration / elapsed) if elapsed and duration else 0.0
    log.info(
        "Diarized %d turn(s), %d speaker(s), in %.1f min (%.2fx realtime)",
        len(turns), speaker_count, elapsed / 60.0, speed,
    )

    payload = {
        "model": model_name,
        "device": device,
        "duration": duration,
        "diarize_seconds": round(elapsed, 1),
        "turn_count": len(turns),
        "speaker_count": speaker_count,
        "turns": turns,
    }
    ws.write_json(target, payload)
    log.info("Wrote %s", target)

    ws.mark_stage(
        STAGE, model=model_name, device=device, turns=len(turns), speakers=speaker_count
    )

    # Best-effort seed of speaker_map.json from the fresh turns. Never
    # overwrites an existing assignment (bulk_from_diarization's default),
    # so re-running this stage can't silently discard a human's manual
    # corrections or already-named speakers.
    if ws.transcript_path.exists():
        from .. import speakers as speakers_module

        transcript = ws.read_json(ws.transcript_path)
        speakers_module.bulk_from_diarization(ws, transcript, payload)

    return target
