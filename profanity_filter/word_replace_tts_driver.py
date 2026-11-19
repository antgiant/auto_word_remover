"""word_replace_tts_driver.py -- runs under profanity_filter/.venv-tts
(NOT the shared voice_to_text venv). Invoked as a subprocess by
word_replace_synth.py exactly like clean.py's ensure_vocals_transcript()
invokes voice_to_text/transcribe.py: holds the GPU lock itself for the
call's duration, writes its result to an output file plus a
"<output>.status.json" sidecar, and never raises past its own process
boundary - a failure here is always reported via that status file, never
a bare non-zero exit a caller has to guess the meaning of.

    word_replace_tts_driver.py --job job.json --output out.wav

job.json shapes (see word_replace_synth.py for the Python-side callers):
    {"mode": "tts_only", "text": str, "target_duration_s": float,
     "voice_model_path": null}
    {"mode": "tts_plus_vc", "text": str, "target_duration_s": float,
     "voice_model_path": str}
    {"mode": "train_voice_model", "speaker_training_wav": str, "model_dir": str}

Tool choices (Piper for TTS, RVC for voice conversion) are this project's
current recommendation, not a guaranteed-stable API - verify the installed
package's actual interface against what's called below before relying on
this; pin and adjust as needed. See README.md's "TTS/Voice-Conversion
setup" for the one-time .venv-tts bootstrap.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
GPU_LOCK_DIR = HERE.parent / "gpu_lock"
PIPER_VOICES_DIR = HERE / ".venv-tts" / "piper_voices"
# No real per-speaker voice conversion yet (rvc-python has no training API -
# see word_replace_synth.ensure_voice_model's docstring), but picking a
# same-sex neutral voice instead of one fixed voice for every speaker is a
# real, available improvement - confirmed live 2026-10-21 that using a
# single male voice regardless of the actual speaker read as a plain wrong
# answer, not just an acceptable approximation. Threshold is the
# conventional rough midpoint between typical adult male/female speaking F0
# ranges - a coarse heuristic, not a real classifier.
PIPER_VOICE_MALE = PIPER_VOICES_DIR / "en_US-hfc_male-medium.onnx"
# en_US-lessac-medium was the original default - dropped after a direct
# listening test (2026-10-21) came back "sounds female", and two other
# alternatives (en_US-john-medium, en_US-joe-medium) had audible
# crackling/distortion; hfc_male was the one that sounded right.
PIPER_VOICE_FEMALE = PIPER_VOICES_DIR / "en_US-amy-medium.onnx"
PIPER_VOICE_DEFAULT = PIPER_VOICE_MALE
_F0_GENDER_THRESHOLD_HZ = 165.0


def _pick_piper_voice(target_f0_hz: float | None) -> Path:
    if target_f0_hz is None:
        return PIPER_VOICE_DEFAULT if PIPER_VOICE_DEFAULT.is_file() else PIPER_VOICE_MALE
    voice = PIPER_VOICE_FEMALE if target_f0_hz >= _F0_GENDER_THRESHOLD_HZ else PIPER_VOICE_MALE
    return voice if voice.is_file() else PIPER_VOICE_DEFAULT


def _gpu_lock_ctx(reason: str):
    if str(GPU_LOCK_DIR) not in sys.path:
        sys.path.insert(0, str(GPU_LOCK_DIR))
    try:
        import gpu_lock
    except ImportError:
        return contextlib.nullcontext()
    return gpu_lock.hold("Profanity_Filter", reason)


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _synthesize_piper(text: str, out_wav: Path, length_scale: float = 1.0,
                      target_f0_hz: float | None = None) -> None:
    """Confirmed against piper-tts's real, current docs
    (github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_PYTHON.md, read
    2026-10-20): `PiperVoice.load(path)`, then
    `.synthesize_wav(text, wav_file, syn_config=SynthesisConfig(...))` -
    NOT a bare `.synthesize(text, wav_file, length_scale=...)` call, which
    is what an earlier, unverified pass at this function assumed."""
    from piper import PiperVoice, SynthesisConfig

    voice_path = _pick_piper_voice(target_f0_hz)
    if not voice_path.is_file():
        raise RuntimeError(f"no Piper voice model at {voice_path} - download one with "
                           f"'python -m piper.download_voices <name>' per README.md's TTS setup")
    voice = PiperVoice.load(str(voice_path))
    syn_config = SynthesisConfig(length_scale=length_scale)
    with wave.open(str(out_wav), "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)


def _convert_voice_rvc(src_wav: Path, voice_model_dir: Path, out_wav: Path) -> None:
    """rvc-python's real, confirmed API (pypi.org/project/rvc-python, read
    2026-10-20): `RVCInference(device=...).load_model(path/to/model.pth)`,
    then `.infer_file(src, dst)`. voice_model_dir is expected to hold
    exactly one `*.pth` file (see _train_rvc_model's docstring on why that
    directory is currently always empty in practice)."""
    from rvc_python.infer import RVCInference

    pth_files = sorted(Path(voice_model_dir).glob("*.pth"))
    if not pth_files:
        raise RuntimeError(f"no .pth model file in {voice_model_dir}")
    device = "cuda:0" if _cuda_available() else "cpu"
    rvc = RVCInference(device=device)
    rvc.load_model(str(pth_files[0]))
    rvc.infer_file(str(src_wav), str(out_wav))


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def _train_rvc_model(speaker_wav: Path, model_dir: Path) -> None:
    """**Not currently implemented** - confirmed by reading rvc-python's
    actual PyPI README (2026-10-20): that package is INFERENCE ONLY. It
    loads a pre-existing `.pth` model file; it has no training/fitting
    entry point of any kind. Real RVC model training (fitting a NEW voice
    from a short sample) needs the full RVC-WebUI-style pipeline (feature
    extraction + many-epoch training - typically real minutes-to-hours,
    not something that fits this per-file automated flow without a lot
    more infrastructure) or a different, zero-shot-capable voice-conversion
    tool chosen specifically for that property.

    Deliberately raises rather than silently pretending to train something -
    the caller (ensure_voice_model) catches this exactly like any other
    training failure and falls back to a neutral, un-converted voice for
    that speaker's synthesis candidates, which is a real and useful
    degraded mode, not a crash. See AGENTS.md's "Word replacement" known
    limitations for the honest status of this piece."""
    raise NotImplementedError(
        "rvc-python has no training API - per-speaker voice-conversion model "
        "training is not implemented; synthesis falls back to a neutral voice")


def do_tts(text: str, target_duration_s: float, out_wav: Path,
          target_f0_hz: float | None = None) -> dict:
    with _gpu_lock_ctx(f"TTS synth: {text[:40]!r}"):
        _synthesize_piper(text, out_wav, target_f0_hz=target_f0_hz)
    actual = _wav_duration(out_wav)
    if target_duration_s > 0 and actual > 0:
        ratio = actual / target_duration_s
        if ratio < 0.8 or ratio > 1.2:
            # One corrective re-run with an adjusted length_scale (Piper's
            # own duration-control knob: "< 1 is faster, > 1 is slower" per
            # piper/config.py). ratio > 1 means the FIRST pass came out too
            # LONG, which calls for a SHORTER (length_scale < 1) retry - i.e.
            # the correction factor is 1/ratio, not ratio itself. Passing
            # ratio directly (as an earlier version of this function did)
            # pushed the retry in the wrong direction every time - too long
            # got rendered even longer, too short even shorter - leaving the
            # final ffmpeg atempo fit (word_replace.fit_duration, which
            # always runs regardless of candidate source) to close a much
            # larger gap than necessary; confirmed live 2026-10-21 as a
            # likely source of an unnaturally pitched-sounding replacement
            # word (atempo preserves true pitch, but very large compression
            # ratios still degrade voice quality in a way that reads as
            # "pitched up" to a listener). A single retry, not a search loop.
            with _gpu_lock_ctx(f"TTS synth (retry): {text[:40]!r}"):
                _synthesize_piper(text, out_wav, length_scale=1.0 / ratio, target_f0_hz=target_f0_hz)
            actual = _wav_duration(out_wav)
    return {"actual_duration_s": actual}


def do_tts_plus_vc(job: dict, out_wav: Path, tmp_dir: Path) -> dict:
    raw = tmp_dir / f"{out_wav.stem}_pretrain.wav"
    do_tts(job["text"], float(job.get("target_duration_s") or 0.0), raw,
          target_f0_hz=job.get("target_f0_hz"))
    voice_model_path = job.get("voice_model_path")
    if not voice_model_path:
        out_wav.write_bytes(raw.read_bytes())
        return {"actual_duration_s": _wav_duration(out_wav)}
    with _gpu_lock_ctx("RVC voice conversion"):
        _convert_voice_rvc(raw, Path(voice_model_path), out_wav)
    return {"actual_duration_s": _wav_duration(out_wav)}


def do_train(job: dict) -> dict:
    speaker_wav = Path(job["speaker_training_wav"])
    model_dir = Path(job["model_dir"])
    with _gpu_lock_ctx(f"RVC training: {model_dir.name}"):
        _train_rvc_model(speaker_wav, model_dir)
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="word_replace_tts_driver.py")
    ap.add_argument("--job", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)

    out_wav = Path(args.output)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    status_path = out_wav.with_suffix(out_wav.suffix + ".status.json")

    try:
        job = json.loads(Path(args.job).read_text(encoding="utf-8"))
        mode = job.get("mode")
        if mode == "tts_only":
            extra = do_tts(job["text"], float(job.get("target_duration_s") or 0.0), out_wav,
                          target_f0_hz=job.get("target_f0_hz"))
        elif mode == "tts_plus_vc":
            extra = do_tts_plus_vc(job, out_wav, out_wav.parent)
        elif mode == "train_voice_model":
            extra = do_train(job)
        else:
            raise ValueError(f"unknown job mode: {mode!r}")
    except Exception as exc:
        status_path.write_text(json.dumps({"ok": False, "reason": repr(exc)}), encoding="utf-8")
        print(f"[error] word_replace_tts_driver: {exc!r}", file=sys.stderr)
        return 1

    status_path.write_text(json.dumps({"ok": True, **extra}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
