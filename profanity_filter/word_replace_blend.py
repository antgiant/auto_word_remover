"""word_replace_blend.py -- scene-matched loudness/EQ/reverb blending for
--method replace (word_replace.py), so a replacement word's audio doesn't
sound dry/pasted-in against the original scene's own ambience.

Pure ffmpeg (astats/volume/highpass/lowpass/aecho) - no new dependency.
Concrete, not research-grade: loudness/EQ matching is a plain gain/band
match against the clip's immediate neighbours, and the reverb step is a
crude decay-time proxy, NOT a real room-impulse-response match - the
single lowest-confidence piece of the whole --method replace feature (see
AGENTS.md). Every function degrades to passing its input through unchanged
on any ffmpeg failure - never raises past this boundary.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_RMS_RE = re.compile(r"RMS level dB:\s*(-?\d+(?:\.\d+)?|-inf)")


def _mean_rms_db(ffmpeg: str, wav_path: Path) -> float | None:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(wav_path), "-af", "astats=measure_overall=RMS_level",
             "-f", "null", "-"],
            capture_output=True, text=True)
        matches = _RMS_RE.findall(proc.stderr)
        vals = [float(m) for m in matches if m != "-inf"]
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None


def match_loudness(candidate_wav: Path, neighbor_before: Path, neighbor_after: Path,
                   out_wav: Path, ffmpeg: str) -> Path:
    """Match candidate_wav's RMS level to the average of its two immediate
    neighbours' RMS levels via a plain volume=<gain>dB filter - not
    loudnorm, whose EBU R128 integrated measurement needs several seconds
    to be reliable and is a poor fit for a sub-second word clip."""
    ref_vals = [v for v in (_mean_rms_db(ffmpeg, neighbor_before), _mean_rms_db(ffmpeg, neighbor_after))
               if v is not None]
    cand_db = _mean_rms_db(ffmpeg, candidate_wav)
    if not ref_vals or cand_db is None:
        _copy(ffmpeg, candidate_wav, out_wav)
        return out_wav
    ref_db = sum(ref_vals) / len(ref_vals)
    gain_db = max(-24.0, min(24.0, ref_db - cand_db))
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(candidate_wav),
                       "-af", f"volume={gain_db:.3f}dB", str(out_wav)],
                      check=True, capture_output=True)
    except subprocess.CalledProcessError:
        _copy(ffmpeg, candidate_wav, out_wav)
    return out_wav


_BANDS = [("low", "lowpass=f=300"), ("mid", "highpass=f=300,lowpass=f=3000"),
         ("high", "highpass=f=3000")]


def match_eq_approx(candidate_wav: Path, neighbor_before: Path, neighbor_after: Path,
                   out_wav: Path, ffmpeg: str) -> Path:
    """Coarse 3-band (low/mid/high) gain match - split both the reference
    neighbours and the candidate into the same three bands, compare each
    band's RMS energy, and apply a matching per-band gain to the
    candidate, recombined. An approximation, not a parametric EQ fit."""
    try:
        gains = []
        for _name, filt in _BANDS:
            ref_vals = [v for v in (_band_rms(ffmpeg, neighbor_before, filt),
                                    _band_rms(ffmpeg, neighbor_after, filt)) if v is not None]
            cand_db = _band_rms(ffmpeg, candidate_wav, filt)
            if not ref_vals or cand_db is None:
                gains.append(0.0)
                continue
            ref_db = sum(ref_vals) / len(ref_vals)
            gains.append(max(-12.0, min(12.0, ref_db - cand_db)))

        filter_parts = []
        for (_name, filt), gain in zip(_BANDS, gains):
            filter_parts.append(f"[0:a]{filt},volume={gain:.3f}dB[{_name}]")
        mix = "".join(filter_parts) + "[low][mid][high]amix=inputs=3:normalize=0[out]"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(candidate_wav),
                       "-filter_complex", mix, "-map", "[out]", str(out_wav)],
                      check=True, capture_output=True)
    except Exception:
        _copy(ffmpeg, candidate_wav, out_wav)
    return out_wav


def _band_rms(ffmpeg: str, wav_path: Path, filt: str) -> float | None:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(wav_path), "-af",
             f"{filt},astats=measure_overall=RMS_level", "-f", "null", "-"],
            capture_output=True, text=True)
        matches = _RMS_RE.findall(proc.stderr)
        vals = [float(m) for m in matches if m != "-inf"]
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None


def estimate_ambient_decay(instrumental_clip_near_span: Path, ffmpeg: str, ffprobe: str) -> float:
    """Crude RT20-style proxy, NOT a real room-impulse-response estimate:
    measure how long the ambient/instrumental signal just after the span
    takes to fall ~20dB from its own peak level, and treat that as the
    scene's rough decay time. Returns a small default (0.15s) on any
    failure - a mild, safe fallback rather than no reverb at all."""
    default = 0.15
    try:
        dur_out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(instrumental_clip_near_span)],
            capture_output=True, text=True, check=True).stdout.strip()
        dur = float(dur_out)
        if dur <= 0:
            return default
        window = max(0.02, min(0.05, dur / 10.0))
        n_windows = max(1, int(dur / window))
        tmp_dir = instrumental_clip_near_span.parent
        levels = []
        for i in range(n_windows):
            s, e = i * window, min(dur, (i + 1) * window)
            win_clip = tmp_dir / f"_decay_win_{i}.wav"
            subprocess.run([ffmpeg, "-hide_banner", "-y", "-ss", f"{s:.6f}", "-to", f"{e:.6f}",
                           "-i", str(instrumental_clip_near_span), str(win_clip)],
                          check=True, capture_output=True)
            db = _mean_rms_db(ffmpeg, win_clip)
            levels.append(db if db is not None else -60.0)
        peak = max(levels)
        below_20 = [i for i, lv in enumerate(levels) if lv <= peak - 20.0]
        if not below_20:
            return default
        decay_time = below_20[0] * window
        return max(0.05, min(0.6, decay_time))
    except Exception:
        return default


def apply_reverb_blend(candidate_wav: Path, decay_time_s: float, out_wav: Path, ffmpeg: str) -> Path:
    """A short, conservative aecho tail - a proxy for "this scene has some
    ambience", not a real convolution reverb match."""
    delay_ms = max(20, min(400, int(decay_time_s * 1000)))
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(candidate_wav),
                       "-af", f"aecho=0.8:0.7:{delay_ms}:0.18", str(out_wav)],
                      check=True, capture_output=True)
    except subprocess.CalledProcessError:
        _copy(ffmpeg, candidate_wav, out_wav)
    return out_wav


def blend_candidate(candidate_wav: Path, neighbor_before: Path, neighbor_after: Path,
                    instrumental_clip: Path | None, out_wav: Path, ffmpeg: str,
                    ffprobe: str) -> Path:
    """Convenience wrapper chaining loudness -> EQ -> (optional) reverb.
    instrumental_clip=None skips the reverb step entirely (no ambient
    reference available - e.g. no stemmer installed)."""
    tmp_dir = out_wav.parent
    step1 = tmp_dir / f"{out_wav.stem}_loud.wav"
    match_loudness(candidate_wav, neighbor_before, neighbor_after, step1, ffmpeg)
    step2 = tmp_dir / f"{out_wav.stem}_eq.wav"
    match_eq_approx(step1, neighbor_before, neighbor_after, step2, ffmpeg)
    if instrumental_clip is not None:
        decay = estimate_ambient_decay(instrumental_clip, ffmpeg, ffprobe)
        return apply_reverb_blend(step2, decay, out_wav, ffmpeg)
    _copy(ffmpeg, step2, out_wav)
    return out_wav


def _copy(ffmpeg: str, src: Path, dst: Path) -> None:
    import shutil
    if src != dst:
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            print(f"  [warn] replace: blend passthrough copy failed ({exc!r})", file=sys.stderr)
