"""Standalone wrapper around NVIDIA's Parakeet (FastConformer-TDT, nvidia/parakeet-tdt-0.6b-v3)
for clean.py's bare-word hallucination corroboration check (see
flag_language.detect_bare_hallucination_word_candidates / clean.py's
parakeet_second_opinion_rescue).

Why Parakeet instead of a second WhisperX checkpoint: confirmed live 2026-11-20 on four real
narration-heavy titles (God of Wonders (2008), Final Events of Bible Prophecy, Of Penguins and
Men (2005), Disneynature's Wings of Life (2011)) that Whisper's own cross-checkpoint "second
opinion" (a DIFFERENT WhisperX model size, same architecture/training lineage) shares the exact
same decoder bias on this content - it reproduced the identical hallucinated text (same words,
sometimes verbatim) as the original pass in every case tested, including when re-transcribing an
ISOLATED VOCALS stem instead of the raw mix. Parakeet is a structurally different ASR
architecture (FastConformer encoder + TDT decoder, not Whisper-derived at all) and, tested
against the exact same clips, correctly produced EMPTY output for the hallucinated windows while
still correctly transcribing real narration in the same file - a genuinely independent signal
Whisper-vs-Whisper can't give.

Deliberately a SEPARATE venv (.venv-parakeet, sibling to .venv-stem) rather than added to
voice_to_text's own carefully-pinned environment: transformers' own Parakeet support needs a
newer transformers than this project's main WhisperX/pyannote stack is pinned to, and there's no
reason to risk that stack's pinned versions for an optional corroboration check that already
degrades gracefully to "not installed" everywhere else in this codebase.

Usage: parakeet_transcribe.py <input.wav> [<input2.wav> ...] [--formats json|text] [--output-dir DIR]
Writes "<input stem>.json" ({"text": "..."}) or "<input stem>.txt" (plain text) next to each
input, or into --output-dir if given - one output file per input, same as voice_to_text/
transcribe.py's own CLI shape, so clean.py's existing per-clip output-reading code needs no
changes.

Accepts MULTIPLE inputs in one call on purpose (2026-11-22): the model loads once regardless of
input count (~2-3s, confirmed) and transcribes a whole list in one `pipeline()` call - batching
every hit a single file needs checked into ONE subprocess call instead of one per hit cuts the
dominant cost (repeated model-load overhead) by the hit count. Added after finding the
per-hit-subprocess design (parakeet_second_opinion_rescue/parakeet_audit_combined_mutes's
original shape) would make auditing the ~343-file hallucination-fix backlog take many hours of
pure reload overhead alone - see those two functions' own callers for the batching rewrite."""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", type=Path, nargs="+")
    ap.add_argument("--formats", default="json", choices=["json", "text"])
    ap.add_argument("--output-dir", type=Path, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    import torch
    from transformers import pipeline

    gpu_ctx = contextlib.nullcontext()
    if torch.cuda.is_available():
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gpu_lock"))
        try:
            import gpu_lock
            names = ", ".join(p.name for p in args.inputs[:3]) + (
                "..." if len(args.inputs) > 3 else "")
            gpu_ctx = gpu_lock.hold("Parakeet", f"transcribing {names}")
        except ImportError:
            pass  # gpu_lock is optional - only useful if you have other GPU tools to share with

    if not args.quiet:
        print(f"loading {MODEL_ID}...", file=sys.stderr)
    device = 0 if torch.cuda.is_available() else -1
    with gpu_ctx:
        asr = pipeline("automatic-speech-recognition", model=MODEL_ID, device=device)
        results = asr([str(p) for p in args.inputs])
    if isinstance(results, dict):
        results = [results]  # a single input still comes back as one dict, not a 1-list

    for inp, result in zip(args.inputs, results):
        text = result["text"] if isinstance(result, dict) else str(result)
        out_dir = args.output_dir or inp.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.formats == "json":
            out_path = out_dir / f"{inp.stem}.json"
            out_path.write_text(json.dumps({"text": text}, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        else:
            out_path = out_dir / f"{inp.stem}.txt"
            out_path.write_text(text, encoding="utf-8")
        if not args.quiet:
            print(f"wrote: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
