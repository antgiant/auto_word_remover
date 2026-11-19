"""word_replace_prosody.py -- pitch/energy contour extraction + imposition
for --method replace (word_replace.py), so a replacement word's emotional
delivery (shouting, crying, flat exclamation, ...) matches the ORIGINAL
word's rather than whatever a TTS/donor rendering happens to carry on its
own.

Uses the WORLD vocoder via `pyworld` - needs `pyworld`/`numpy`/`soundfile`
installed (part of the `.venv-tts` bootstrap, see AGENTS.md / README's
"TTS/Voice-Conversion setup"); every public function degrades to a no-op
(returns None / copies the input through unchanged) when those aren't
importable, rather than raising - word_replace.py already treats a None
contour or a pass-through output as "skip this step", matching the
degrade-gracefully posture used everywhere else in this project.
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

FRAME_PERIOD_MS = 5.0  # pyworld's dio/cheaptrick/d4c frame hop


@dataclasses.dataclass
class ProsodyContour:
    f0: "object"            # np.ndarray, one F0 value (Hz, 0 = unvoiced) per frame
    energy: "object"         # np.ndarray, one RMS energy value per frame (same frame grid as f0)
    frame_period_ms: float
    duration_s: float


def extract_prosody(wav_path: Path) -> ProsodyContour | None:
    """None if pyworld/numpy/soundfile aren't installed, or extraction
    fails for any reason (e.g. a clip too short/silent to analyze) - never
    raises past this boundary."""
    try:
        import numpy as np
        import pyworld as pw
        import soundfile as sf
    except ImportError:
        return None
    try:
        x, sr = sf.read(str(wav_path))
        if x.ndim > 1:
            x = x.mean(axis=1)
        x = np.ascontiguousarray(x, dtype=np.float64)
        if x.size == 0:
            return None
        f0, t = pw.dio(x, sr, frame_period=FRAME_PERIOD_MS)
        f0 = pw.stonemask(x, f0, t, sr)
        hop = int(round(sr * FRAME_PERIOD_MS / 1000.0))
        energy = np.array([
            float(np.sqrt(np.mean(np.square(x[i:i + hop])))) if x[i:i + hop].size else 0.0
            for i in range(0, max(1, x.size), hop)
        ])
        n = min(len(f0), len(energy))
        if n == 0:
            return None
        return ProsodyContour(f0=f0[:n], energy=energy[:n], frame_period_ms=FRAME_PERIOD_MS,
                              duration_s=x.size / sr)
    except Exception as exc:
        print(f"  [warn] replace: prosody extraction failed on {wav_path.name} ({exc!r})",
              file=sys.stderr)
        return None


def apply_prosody(candidate_wav: Path, contour: ProsodyContour, out_wav: Path) -> Path:
    """Decompose candidate_wav, replace its F0 track with `contour`'s
    (resampled onto the candidate's own frame grid - the candidate is
    already duration-matched to the target span by the time this runs, so
    a straight index-proportional resample is enough), scale its energy
    frame-by-frame to match `contour`'s, and resynthesize. Falls back to
    copying candidate_wav through unchanged (returns out_wav either way) on
    any failure - never raises past this boundary, matching every other
    optional step in this feature."""
    try:
        import numpy as np
        import pyworld as pw
        import soundfile as sf
    except ImportError:
        shutil.copy2(candidate_wav, out_wav)
        return out_wav
    try:
        x, sr = sf.read(str(candidate_wav))
        if x.ndim > 1:
            x = x.mean(axis=1)
        x = np.ascontiguousarray(x, dtype=np.float64)
        if x.size == 0:
            shutil.copy2(candidate_wav, out_wav)
            return out_wav

        f0, t = pw.dio(x, sr, frame_period=contour.frame_period_ms)
        f0 = pw.stonemask(x, f0, t, sr)
        sp = pw.cheaptrick(x, f0, t, sr)
        ap = pw.d4c(x, f0, t, sr)

        n_cand = len(f0)
        n_src = len(contour.f0)
        if n_cand == 0 or n_src == 0:
            shutil.copy2(candidate_wav, out_wav)
            return out_wav
        idx = np.clip((np.arange(n_cand) * n_src / n_cand).astype(int), 0, n_src - 1)
        new_f0 = contour.f0[idx].copy()
        # A candidate frame the original marked unvoiced (f0==0) stays
        # unvoiced regardless of what the source contour says at that
        # resampled position, and vice versa - only rescale/replace pitch
        # where BOTH sides agree voicing is present, else keep the
        # candidate's own (0 or voiced) value so silence/unvoiced
        # consonants aren't forced into a tone.
        voiced_both = (new_f0 > 0) & (f0 > 0)
        out_f0 = f0.copy()
        out_f0[voiced_both] = new_f0[voiced_both]

        new_energy = contour.energy[idx]
        cand_energy = np.array([
            float(np.sqrt(np.mean(np.square(
                x[int(i * sr * contour.frame_period_ms / 1000.0):
                  int((i + 1) * sr * contour.frame_period_ms / 1000.0)]))))
            if x.size else 0.0
            for i in range(n_cand)
        ])
        gain = np.ones(n_cand)
        nonzero = cand_energy > 1e-6
        gain[nonzero] = np.clip(new_energy[nonzero] / cand_energy[nonzero], 0.25, 4.0)

        y = pw.synthesize(out_f0, sp, ap, sr, contour.frame_period_ms)
        # Apply the per-frame gain as a simple piecewise-constant envelope
        # over the resynthesized waveform - approximate (ignores any
        # resynthesis length drift from the frame grid) but good enough for
        # a sub-second word clip, and far simpler than a sample-accurate
        # frame-to-sample energy warp.
        hop = max(1, int(round(sr * contour.frame_period_ms / 1000.0)))
        env = np.repeat(gain, hop)[:len(y)]
        if len(env) < len(y):
            env = np.pad(env, (0, len(y) - len(env)), mode="edge")
        y = y * env

        sf.write(str(out_wav), y, sr)
        return out_wav
    except Exception as exc:
        print(f"  [warn] replace: prosody imposition failed on {candidate_wav.name} ({exc!r}) - "
              f"using the candidate's own natural prosody instead", file=sys.stderr)
        shutil.copy2(candidate_wav, out_wav)
        return out_wav
