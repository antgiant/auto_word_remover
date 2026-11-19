"""transcribe_clip_batch.py -- batched, lightweight re-transcription of many
short candidate clips in ONE process, for profanity_filter's --method
replace (word_replace_score.py's run_batch_asr). Loads WhisperX exactly
once regardless of how many clips are in the manifest, so scoring a whole
file's worth of candidate renders costs one model load, not one per
candidate.

Deliberately simpler than transcribe.py: no forced alignment, no
diarization, no multi-format output - just "what did this short clip most
likely say", which is all candidate scoring needs. Lives here (not in
profanity_filter/) because it needs WhisperX, which only voice_to_text's
venv has installed.

    transcribe_clip_batch.py --manifest clips.json --output-dir <tmp> \
        --model small --device cuda

clips.json: [{"id": "c0", "path": "C:\\...\\cand0.wav"}, ...]
Writes <output-dir>/<id>.json = {"text": "..."} for each clip that
transcribed successfully; a clip that fails is simply skipped (the caller,
word_replace_score.run_batch_asr, treats a missing id as "no text" /
intelligibility 0, not an error).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="transcribe_clip_batch.py")
    p.add_argument("--manifest", required=True, help="JSON file: [{id, path}, ...]")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--model", default="small")
    p.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    p.add_argument("--compute-type", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    manifest_path = Path(args.manifest)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        clips = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[error] couldn't read manifest {manifest_path}: {exc!r}", file=sys.stderr)
        return 2
    if not clips:
        return 0

    import torch
    device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        print("[warn] CUDA not available -> using CPU", file=sys.stderr)
        device = "cpu"
    compute_type = args.compute_type or ("float16" if device == "cuda" else "int8")

    gpu_ctx = contextlib.nullcontext()
    if device == "cuda":
        sys.path.insert(0, str(HERE.parent / "gpu_lock"))
        try:
            import gpu_lock
            gpu_ctx = gpu_lock.hold("Profanity_Filter",
                                    f"scoring {len(clips)} replace candidate(s)")
        except ImportError:
            pass

    import whisperx

    written = 0
    with gpu_ctx:
        try:
            asr_model = whisperx.load_model(
                args.model, device=device, compute_type=compute_type,
                download_root=str(HERE / "models"))
        except Exception as exc:
            print(f"[error] couldn't load WhisperX model {args.model!r}: {exc!r}", file=sys.stderr)
            return 1

        for clip in clips:
            cid, path = clip.get("id"), clip.get("path")
            if not cid or not path or not Path(path).is_file():
                continue
            try:
                audio = whisperx.load_audio(str(path))
                result = asr_model.transcribe(audio, batch_size=1, print_progress=False,
                                              combined_progress=False)
                text = " ".join((seg.get("text") or "").strip() for seg in result.get("segments", []))
                (out_dir / f"{cid}.json").write_text(json.dumps({"text": text.strip()}),
                                                     encoding="utf-8")
                written += 1
            except Exception as exc:
                print(f"  [warn] transcribe_clip_batch: clip {cid!r} failed ({exc!r}) - skipped",
                      file=sys.stderr)

    print(f"transcribed {written}/{len(clips)} candidate clip(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
