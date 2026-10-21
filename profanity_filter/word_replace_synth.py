"""word_replace_synth.py -- two-stage synthesis (neutral TTS -> per-speaker
voice conversion) for --method replace (word_replace.py), when no usable
donor clip exists elsewhere in the file.

Runs the actual model inference in a SEPARATE venv (.venv-tts, same
dedicated-venv convention as clean.py's own .venv-stem for audio-separator -
see AGENTS.md) via word_replace_tts_driver.py, invoked as a subprocess
exactly like clean.py's ensure_vocals_transcript() invokes
voice_to_text/transcribe.py: a driver script that holds the GPU lock itself
for the call's duration, writes its result to an output file (never stdout
parsing), and never raises past its own process boundary - a missing
.venv-tts, a failed driver run, or not enough source audio to train a voice
model all degrade to "no candidate produced" (None), not a crash.

Tool choices below (Piper for TTS, RVC for voice conversion) are this
plan's recommendation, not a hard requirement - verify current package
name/availability/license before installing; this ecosystem moves fast.
See README.md's "TTS/Voice-Conversion setup" for the one-time bootstrap.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# A dedicated venv for the TTS/voice-conversion/F0 stack (Piper + RVC +
# pyworld), kept separate from voice_to_text/.venv and .venv-stem for the
# same reason those are separate from each other: its own torch/CUDA pins
# should never touch either of theirs. Not auto-installed - see README.
TTS_VENV = HERE / ".venv-tts"
_DRIVER_SCRIPT = HERE / "word_replace_tts_driver.py"


def locate_tts_driver() -> Path | None:
    """The .venv-tts python.exe, if both the venv and the driver script
    exist - None (meaning "skip synthesis entirely for this run") when
    either is missing, same degrade-gracefully posture as
    clean.py's locate_stem_tool()."""
    py = TTS_VENV / "Scripts" / "python.exe"
    if py.is_file() and _DRIVER_SCRIPT.is_file():
        return py
    return None


def _run_driver(driver_py: Path, job: dict, out_wav: Path, tmp: Path, tag: str) -> dict | None:
    job_path = tmp / f"tts_job_{tag}.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    status_path = out_wav.with_suffix(out_wav.suffix + ".status.json")
    try:
        subprocess.run([str(driver_py), "-X", "utf8", str(_DRIVER_SCRIPT),
                       "--job", str(job_path), "--output", str(out_wav)],
                      check=True, cwd=str(HERE), capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        print(f"  [warn] replace: TTS driver failed for job {tag!r}: "
              f"{(exc.stderr or '').strip()[-500:]}", file=sys.stderr)
        return None
    if not status_path.is_file():
        return None
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not status.get("ok"):
        print(f"  [warn] replace: TTS driver reported failure for job {tag!r}: "
              f"{status.get('reason')}", file=sys.stderr)
        return None
    return status


def synthesize_text(text: str, target_duration_s: float, voice_model_path: Path | None,
                    driver_py: Path, tmp: Path, tag: str,
                    target_f0_hz: float | None = None) -> Path | None:
    """Neutral (or voice-converted, if voice_model_path is given) TTS
    rendering of `text`, roughly duration-matched to target_duration_s
    (the driver applies one length_scale pass plus a single corrective
    re-run - final exact fitting still happens via word_replace.fit_duration
    afterward, same as a donor clip gets). None on any failure.

    target_f0_hz: the original word's own median pitch, if known - lets the
    driver pick a same-sex neutral voice instead of one fixed voice
    regardless of the actual speaker (see word_replace.render_synth_candidate's
    docstring; real per-speaker voice conversion isn't available yet)."""
    out_wav = tmp / f"synth_{tag}_raw.wav"
    mode = "tts_plus_vc" if voice_model_path is not None else "tts_only"
    job = {
        "mode": mode,
        "text": text,
        "target_duration_s": target_duration_s,
        "voice_model_path": str(voice_model_path) if voice_model_path else None,
        "target_f0_hz": target_f0_hz,
    }
    status = _run_driver(driver_py, job, out_wav, tmp, tag)
    if status is None or not out_wav.is_file():
        return None
    return out_wav


_VOICE_MODEL_CACHE: dict[str, Path | None] = {}


def ensure_voice_model(media: Path, cfg, speaker_key: str, speaker_training_wav: Path | None,
                       tmp: Path, driver_py: Path) -> Path | None:
    """A per-speaker voice-conversion model, trained once per speaker per
    film from speaker_training_wav (that speaker's own isolated vocal
    audio - see word_replace._speaker_training_wav), cached next to the
    media so a rerun doesn't retrain it. Returns None (meaning "synthesize
    with a neutral, un-converted voice instead") when there's no training
    audio, not enough of it (cfg.replace_min_speaker_audio_s), or training
    fails for any reason - a missing voice model is a quality degradation,
    never a hard failure."""
    safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in speaker_key)
    cache_key = f"{media}::{safe_key}"
    if cache_key in _VOICE_MODEL_CACHE and not cfg.retrain_voice_model:
        return _VOICE_MODEL_CACHE[cache_key]

    meta_path = media.with_name(f"{media.stem}.{safe_key}.vc_model.meta.json")
    model_dir = media.with_name(f"{media.stem}.{safe_key}.vc_model")
    if meta_path.is_file() and model_dir.is_dir() and not cfg.retrain_voice_model:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("trained"):
                _VOICE_MODEL_CACHE[cache_key] = model_dir
                return model_dir
        except (OSError, json.JSONDecodeError):
            pass

    if speaker_training_wav is None or not speaker_training_wav.is_file():
        _VOICE_MODEL_CACHE[cache_key] = None
        return None

    duration_s = _probe_wav_duration(speaker_training_wav)
    if duration_s < cfg.replace_min_speaker_audio_s:
        print(f"  [note] replace: only {duration_s:.1f}s of isolated audio for speaker "
              f"{speaker_key!r} (need {cfg.replace_min_speaker_audio_s:.0f}s) - synthesizing "
              f"with a neutral voice for their occurrences instead", file=sys.stderr)
        _VOICE_MODEL_CACHE[cache_key] = None
        return None

    model_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "mode": "train_voice_model",
        "speaker_training_wav": str(speaker_training_wav),
        "model_dir": str(model_dir),
    }
    status_wav = tmp / f"vc_train_{safe_key}.wav"  # unused placeholder output path the driver
    #   protocol expects - training doesn't produce audio, just the model directory
    status = _run_driver(driver_py, job, status_wav, tmp, f"train_{safe_key}")
    trained = bool(status and status.get("ok"))
    meta_path.write_text(json.dumps({
        "speaker_key": speaker_key, "trained": trained, "source_duration_s": duration_s,
    }, indent=2), encoding="utf-8")
    result = model_dir if trained else None
    _VOICE_MODEL_CACHE[cache_key] = result
    return result


def _probe_wav_duration(wav_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path)],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError):
        return 0.0
