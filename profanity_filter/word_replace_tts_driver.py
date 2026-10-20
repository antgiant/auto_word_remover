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
PIPER_VOICE_PATH = HERE / ".venv-tts" / "piper_voices" / "en_US-amy-medium.onnx"


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


def _synthesize_piper(text: str, out_wav: Path, length_scale: float = 1.0) -> None:
    """Targets the documented piper-tts Python API
    (PiperVoice.load(...).synthesize(text, wav_file, ...)) - verify this
    against the actually-installed version, which may have shifted."""
    from piper import PiperVoice

    if not PIPER_VOICE_PATH.is_file():
        raise RuntimeError(f"no Piper voice model at {PIPER_VOICE_PATH} - download one per "
                           f"README.md's TTS setup section")
    voice = PiperVoice.load(str(PIPER_VOICE_PATH))
    with wave.open(str(out_wav), "wb") as wav_file:
        voice.synthesize(text, wav_file, length_scale=length_scale)


def _convert_voice_rvc(src_wav: Path, voice_model_dir: Path, out_wav: Path) -> None:
    """Targets a documented single-file RVC inference call - verify
    against the actually-installed rvc-python (or Applio-style) package;
    its exact import path/API has moved before and may move again."""
    from rvc_python.infer import RVCInference

    rvc = RVCInference()
    rvc.load_model(str(voice_model_dir))
    rvc.infer_file(str(src_wav), str(out_wav))


def _train_rvc_model(speaker_wav: Path, model_dir: Path) -> None:
    """Targets a documented RVC training/fitting entry point for a small
    per-speaker model from one source wav - verify against the installed
    package; this is the least standardized part of the RVC ecosystem."""
    from rvc_python.train import train_from_wav

    train_from_wav(str(speaker_wav), str(model_dir))


def do_tts(text: str, target_duration_s: float, out_wav: Path) -> dict:
    with _gpu_lock_ctx(f"TTS synth: {text[:40]!r}"):
        _synthesize_piper(text, out_wav)
    actual = _wav_duration(out_wav)
    if target_duration_s > 0 and actual > 0:
        ratio = actual / target_duration_s
        if ratio < 0.8 or ratio > 1.2:
            # One corrective re-run with an adjusted length_scale (Piper's
            # own duration-control knob) - a single retry, not a search
            # loop; residual error beyond this is closed by the final
            # ffmpeg atempo fit word_replace.fit_duration() always applies
            # regardless of candidate source.
            with _gpu_lock_ctx(f"TTS synth (retry): {text[:40]!r}"):
                _synthesize_piper(text, out_wav, length_scale=ratio)
            actual = _wav_duration(out_wav)
    return {"actual_duration_s": actual}


def do_tts_plus_vc(job: dict, out_wav: Path, tmp_dir: Path) -> dict:
    raw = tmp_dir / f"{out_wav.stem}_pretrain.wav"
    do_tts(job["text"], float(job.get("target_duration_s") or 0.0), raw)
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
            extra = do_tts(job["text"], float(job.get("target_duration_s") or 0.0), out_wav)
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
