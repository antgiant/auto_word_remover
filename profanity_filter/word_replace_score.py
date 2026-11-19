"""word_replace_score.py -- multi-candidate quality gating for --method
replace (word_replace.py): re-transcribes every rendered candidate clip
across the WHOLE file in one batched call (voice_to_text/
transcribe_clip_batch.py), scores each for intelligibility + duration fit,
and picks the best-scoring candidate per flagged occurrence (or None, which
tells word_replace.py to mute that occurrence instead).
"""
from __future__ import annotations

import dataclasses
import difflib
import json
import subprocess
import sys
from pathlib import Path


@dataclasses.dataclass
class CandidateScore:
    intelligibility: float   # difflib ratio against the intended word/phrase, 0-1
    duration_fit: float      # 1 - |actual - target| / target, clipped to [0, 1]
    combined: float          # 0.7*intelligibility + 0.3*duration_fit


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def score_candidate(asr_text: str, intended_word: str, actual_duration_s: float,
                   target_duration_s: float) -> CandidateScore:
    intelligibility = difflib.SequenceMatcher(None, _norm(asr_text), _norm(intended_word)).ratio()
    if target_duration_s > 0:
        duration_fit = max(0.0, 1.0 - abs(actual_duration_s - target_duration_s) / target_duration_s)
    else:
        duration_fit = 0.0
    combined = 0.7 * intelligibility + 0.3 * duration_fit
    return CandidateScore(intelligibility=intelligibility, duration_fit=duration_fit,
                          combined=combined)


def pick_best_candidate(candidates: list, threshold: float):
    """candidates: a list of objects each carrying a `.score` (float|None,
    set by word_replace._score_all_candidates) - returns the
    highest-scoring one at or above `threshold`, else None (-> caller
    mutes this occurrence instead)."""
    scored = [c for c in candidates if c.score is not None]
    if not scored:
        return None
    best = max(scored, key=lambda c: c.score)
    return best if best.score >= threshold else None


def run_batch_asr(clips: list[tuple[str, Path]], tmp: Path, model: str,
                  voice_to_text_dir: Path) -> dict[str, str]:
    """clips: [(clip_id, wav_path), ...]. Returns {clip_id: transcribed_text}
    - an id missing from the result means transcription failed/produced
    nothing for that clip; callers should treat that as intelligibility 0,
    not an error. Degrades to {} (every candidate then scores 0 and the
    whole occurrence falls back to mute) when voice_to_text isn't found or
    the batch subprocess itself fails - never raises past this boundary."""
    if not clips:
        return {}
    py = voice_to_text_dir / ".venv" / "Scripts" / "python.exe"
    script = voice_to_text_dir / "transcribe_clip_batch.py"
    if not py.is_file() or not script.is_file():
        print(f"  [warn] replace: voice_to_text's transcribe_clip_batch.py not found at "
              f"{script} - candidate scoring skipped, every occurrence falls back to mute",
              file=sys.stderr)
        return {}

    batch_dir = tmp / "replace_score_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest = batch_dir / "clips.json"
    manifest.write_text(json.dumps([{"id": cid, "path": str(p)} for cid, p in clips]),
                       encoding="utf-8")
    try:
        subprocess.run([str(py), "-X", "utf8", str(script), "--manifest", str(manifest),
                       "--output-dir", str(batch_dir), "--model", model],
                      check=True, cwd=str(voice_to_text_dir))
    except subprocess.CalledProcessError as exc:
        print(f"  [warn] replace: batched candidate re-transcription failed ({exc!r}) - "
              f"candidate scoring skipped, every occurrence falls back to mute", file=sys.stderr)
        return {}

    out: dict[str, str] = {}
    for cid, _p in clips:
        result_path = batch_dir / f"{cid}.json"
        if not result_path.is_file():
            continue
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            out[cid] = data.get("text", "")
        except (OSError, json.JSONDecodeError):
            continue
    return out
