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

Usage: parakeet_transcribe.py <input.wav> [--formats json|text] [--output-dir DIR]
Writes "<input stem>.json" ({"text": "..."}) or "<input stem>.txt" (plain text) next to the
input, or into --output-dir if given. Mirrors voice_to_text/transcribe.py's own CLI shape
closely enough that clean.py's subprocess-invocation code looks the same either way."""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
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
            gpu_ctx = gpu_lock.hold("Parakeet", f"transcribing {args.input.name}")
        except ImportError:
            pass  # gpu_lock is optional - only useful if you have other GPU tools to share with

    if not args.quiet:
        print(f"loading {MODEL_ID}...", file=sys.stderr)
    device = 0 if torch.cuda.is_available() else -1
    with gpu_ctx:
        asr = pipeline("automatic-speech-recognition", model=MODEL_ID, device=device)
        result = asr(str(args.input))
    text = result["text"] if isinstance(result, dict) else str(result)

    out_dir = args.output_dir or args.input.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.formats == "json":
        out_path = out_dir / f"{args.input.stem}.json"
        out_path.write_text(json.dumps({"text": text}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    else:
        out_path = out_dir / f"{args.input.stem}.txt"
        out_path.write_text(text, encoding="utf-8")
    if not args.quiet:
        print(f"wrote: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
