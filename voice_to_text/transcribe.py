#!/usr/bin/env python
"""
Voice_to_Text - high-quality local speech-to-text.

WhisperX (faster-whisper / CTranslate2) transcription
  + wav2vec2 forced alignment  -> exact word-level timestamps
  + token-free pyannote 3.1     -> speaker diarization
  + full CUDA acceleration on your GPU.

Typical use:
    python transcribe.py "meeting.m4a"
    python transcribe.py "a.mp3" "b.wav" --model large-v2 --max-speakers 3
    python transcribe.py "call.mp4" --emit speakers      # print result to stdout

Outputs (written next to the input unless --output-dir is given):
    <name>.txt           plain text, one line per segment
    <name>.json          full WhisperX result (segments + per-word start/end/score/speaker)
    <name>.srt           subtitles
    <name>.vtt           web captions
    <name>.tsv           start<TAB>end<TAB>text
    <name>.words.json    flat list of every word: {word,start,end,score,speaker}
    <name>.speakers.txt  readable transcript grouped by speaker turn

Configuration defaults live in config.toml next to this file; every one can be
overridden on the command line.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*torchcodec.*")
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")
warnings.filterwarnings("ignore", message=".*Lightning automatically upgraded.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pyannote")

HERE = Path(__file__).resolve().parent

# English gets the stronger wav2vec2 LARGE (LV-60k) aligner; it lives in models/
EN_ALIGN_MODEL = "WAV2VEC2_ASR_LARGE_LV60K_960H"

# ---------------------------------------------------------------------------
# Self-contained by default: caches live under this project's own folder
# instead of the usual ~/.cache so a sync client (OneDrive/Dropbox/etc.) can't
# offload multi-GB model weights mid-run. Set HF_HOME/TORCH_HOME yourself
# before running this if you'd rather share a cache with other tools -
# os.environ.setdefault() below won't override an env var you've already set.
# (Must happen before torch / whisperx / huggingface_hub are imported.)
# ---------------------------------------------------------------------------
os.environ.setdefault("HF_HOME", str(HERE / "hf_cache"))
os.environ.setdefault("TORCH_HOME", str(HERE / "torch_cache"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL_DIR = HERE / "models"

AUDIO_EXTS = {".mp3", ".mpa", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".alac"}
VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".avi", ".ts", ".mpg", ".mpeg", ".m2ts", ".flv", ".mov", ".webm", ".wmv", ".vob", ".m2v", ".3gp"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

ALL_FORMATS = ["txt", "json", "srt", "vtt", "tsv", "words.json", "speakers.txt"]

# faster-whisper repo ids for names that ship as separate HF repos
MODEL_ALIASES = {
    "turbo": "large-v3-turbo",
}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


@dataclasses.dataclass
class Config:
    model: str = "large-v3-turbo"
    language: str | None = None            # None => autodetect
    device: str = "cuda"
    compute_type: str = "auto"             # auto|float16|int8_float16|int8|float32
    batch_size: int = 8
    threads: int = 8

    align: bool = True
    align_model: str | None = None         # None => whisperx default for the language
    diarize: bool = True
    diarization_model: str = "token-free-pyannote-3.1"
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None

    formats: list[str] = dataclasses.field(default_factory=lambda: list(ALL_FORMATS))
    output_dir: str | None = None
    highlight_words: bool = False
    max_line_width: int | None = None
    max_line_count: int | None = None
    initial_prompt: str | None = None
    suppress_numerals: bool = False
    beam_size: int = 5
    temperature: float = 0.0
    vad_method: str = "pyannote"           # pyannote|silero
    vad_onset: float = 0.1                 # see "Reducing the Whisper miss rate" in AGENTS.md
    vad_offset: float = 0.05
    chunk_size: int = 30
    no_speech_threshold: float = 0.6
    log_prob_threshold: float = -1.0
    compression_ratio_threshold: float = 2.4
    condition_on_previous_text: bool = False
    repetition_penalty: float = 1.15
    no_repeat_ngram_size: int = 3
    hallucination_silence_threshold: float | None = None
    overwrite: bool = False
    print_progress: bool = True


def load_config(path: Path) -> Config:
    cfg = Config()
    if path.is_file() and tomllib is not None:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        known = {f.name for f in dataclasses.fields(Config)}
        for key, val in data.items():
            if key in known:
                setattr(cfg, key, val)
            else:
                print(f"[config] ignoring unknown key: {key}", file=sys.stderr)
    return cfg


# ---------------------------------------------------------------------------
# diarization  (token-free pyannote 3.1 equivalent, running on pyannote.audio 4.x)
# ---------------------------------------------------------------------------
def build_diarizer(device):
    """Instantiate a speaker-diarization pipeline without any HuggingFace token.

    pyannote.audio 4.x defaults to the gated `speaker-diarization-community-1`
    weights (segmentation + embedding + PLDA).  We swap in the openly-licensed
    mirrors of the pyannote 3.1 components and use agglomerative clustering,
    which does not need the gated PLDA file.
    """
    import torch
    import pyannote.audio.pipelines.speaker_diarization as sd_mod
    from pyannote.audio.core.plda import PLDA

    _orig_get_plda = sd_mod.get_plda

    def _safe_get_plda(plda, token=None, cache_dir=None):
        if isinstance(plda, PLDA):
            return plda
        if isinstance(plda, dict) and str(plda.get("checkpoint", "")).endswith("community-1"):
            return None
        if plda in (None, "", "none", "null"):
            return None
        try:
            return _orig_get_plda(plda, token=token, cache_dir=cache_dir)
        except Exception as exc:  # gated / offline -> clustering path that doesn't need it
            print(f"[diarize] PLDA unavailable, continuing without it ({exc!r})", file=sys.stderr)
            return None

    sd_mod.get_plda = _safe_get_plda
    from pyannote.audio.pipelines.speaker_diarization import SpeakerDiarization

    pipe = SpeakerDiarization(
        segmentation="tensorlake/segmentation-3.0",
        embedding="pyannote/wespeaker-voxceleb-resnet34-LM",
        clustering="AgglomerativeClustering",
        plda=None,
        segmentation_batch_size=32,
        embedding_batch_size=32,
        embedding_exclude_overlap=True,
    )
    pipe.instantiate(
        {
            "clustering": {
                "method": "centroid",
                "min_cluster_size": 12,
                "threshold": 0.7045654963945799,
            },
            "segmentation": {"min_duration_off": 0.0},
        }
    )
    return pipe.to(torch.device(device))


def run_diarization(pipe, audio, num_speakers=None, min_speakers=None, max_speakers=None):
    import pandas as pd
    import torch

    data = {"waveform": torch.from_numpy(audio[None, :]), "sample_rate": 16000}
    out = pipe(
        data,
        num_speakers=num_speakers,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )
    annotation = getattr(out, "speaker_diarization", out)
    rows = [
        {"segment": seg, "label": lbl, "speaker": spk, "start": seg.start, "end": seg.end}
        for seg, lbl, spk in annotation.itertracks(yield_label=True)
    ]
    return pd.DataFrame(rows, columns=["segment", "label", "speaker", "start", "end"])


# ---------------------------------------------------------------------------
# output writers
# ---------------------------------------------------------------------------
def write_standard_formats(result, audio_path, out_dir, formats, writer_opts):
    from whisperx.utils import get_writer

    mapping = {"txt": "txt", "json": "json", "srt": "srt", "vtt": "vtt", "tsv": "tsv"}
    for fmt in formats:
        if fmt not in mapping:
            continue
        writer = get_writer(mapping[fmt], str(out_dir))
        writer(result, str(audio_path), writer_opts)


def write_words_json(result, path: Path):
    words = flat_words(result)
    payload = {
        "language": result.get("language"),
        "word_count": len(words),
        "words": words,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return "??:??:??"
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def speakers_text(result) -> str:
    lines: list[str] = []
    cur_spk = None
    buf: list[str] = []
    start_ts = None

    def flush():
        if buf:
            who = cur_spk or "SPEAKER"
            lines.append(f"[{_fmt_ts(start_ts)}] {who}: " + " ".join(buf).strip())

    for seg in result.get("segments", []):
        spk = seg.get("speaker")
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if spk != cur_spk:
            flush()
            cur_spk = spk
            buf = []
            start_ts = seg.get("start")
        buf.append(text)
    flush()
    return "\n\n".join(lines) + "\n"


def flat_words(result) -> list[dict]:
    return [
        {"word": w.get("word", "").strip(), "start": w.get("start"), "end": w.get("end"),
         "score": w.get("score"), "speaker": w.get("speaker")}
        for s in result.get("segments", []) for w in s.get("words", [])
    ]


def write_speakers_txt(result, path: Path):
    path.write_text(speakers_text(result), encoding="utf-8")


def plain_text(result) -> str:
    return "\n".join((s.get("text") or "").strip() for s in result.get("segments", []) if (s.get("text") or "").strip())


# ---------------------------------------------------------------------------
# main pipeline for one file
# ---------------------------------------------------------------------------
def resolve_compute_type(cfg: Config) -> str:
    if cfg.compute_type != "auto":
        return cfg.compute_type
    if cfg.device != "cuda":
        return "float32"
    try:
        import torch

        free, _total = torch.cuda.mem_get_info()
        # large-v3 fp16 needs ~4.5 GB; fall back to int8 when the GPU is busy
        return "float16" if free > 4.0 * 1024**3 else "int8_float16"
    except Exception:
        return "float16"


def transcribe_file(path: Path, cfg: Config, models: dict) -> dict:
    import whisperx

    t0 = time.time()
    print(f"\n=== {path.name} ===", flush=True)
    audio = whisperx.load_audio(str(path))
    dur = len(audio) / 16000.0
    print(f"    duration {dur/60:.1f} min", flush=True)

    # --- transcription ---
    asr_model = models["asr"]
    result = asr_model.transcribe(
        audio,
        batch_size=cfg.batch_size,
        language=cfg.language,
        print_progress=cfg.print_progress,
        combined_progress=False,
    )
    language = result["language"]
    print(f"    transcribed  ({time.time()-t0:.0f}s, lang={language})", flush=True)

    # --- alignment ---
    if cfg.align:
        try:
            if language not in models["align_cache"]:
                align_name = cfg.align_model
                if align_name is None and language.startswith("en"):
                    align_name = EN_ALIGN_MODEL
                models["align_cache"][language] = whisperx.load_align_model(
                    language_code=language,
                    device=cfg.device,
                    model_name=align_name,
                    model_dir=str(MODEL_DIR),
                )
            amodel, ameta = models["align_cache"][language]
            result = whisperx.align(
                result["segments"], amodel, ameta, audio, cfg.device,
                return_char_alignments=False, print_progress=cfg.print_progress,
            )
            result["language"] = language
            print(f"    aligned      ({time.time()-t0:.0f}s)", flush=True)
        except Exception as exc:
            print(f"    [warn] alignment failed ({exc!r}); keeping segment-level times", file=sys.stderr, flush=True)

    # --- diarization ---
    if cfg.diarize:
        try:
            if models.get("diarizer") is None:
                print("    loading diarizer...", flush=True)
                models["diarizer"] = build_diarizer(cfg.device)
            diarize_df = run_diarization(
                models["diarizer"], audio,
                num_speakers=cfg.num_speakers,
                min_speakers=cfg.min_speakers,
                max_speakers=cfg.max_speakers,
            )
            result = whisperx.assign_word_speakers(diarize_df, result)
            n_spk = diarize_df["speaker"].nunique() if len(diarize_df) else 0
            print(f"    diarized     ({time.time()-t0:.0f}s, {n_spk} speakers)", flush=True)
        except Exception as exc:
            print(f"    [warn] diarization failed ({exc!r})", file=sys.stderr, flush=True)

    result.setdefault("language", language)
    result["_meta"] = {
        "source": str(path),
        "duration_sec": round(dur, 3),
        "model": cfg.model,
        "compute_type": models["compute_type"],
        "aligned": cfg.align,
        "diarized": cfg.diarize,
        "elapsed_sec": round(time.time() - t0, 1),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return result


def emit_outputs(path: Path, result: dict, cfg: Config):
    out_dir = Path(cfg.output_dir) if cfg.output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem

    writer_opts = {
        "max_line_width": cfg.max_line_width,
        "max_line_count": cfg.max_line_count,
        "highlight_words": cfg.highlight_words,
        "preserve_segments": True,
    }
    write_standard_formats(result, out_dir / (stem + path.suffix), out_dir, cfg.formats, writer_opts)

    if "words.json" in cfg.formats:
        write_words_json(result, out_dir / f"{stem}.words.json")
    if "speakers.txt" in cfg.formats:
        write_speakers_txt(result, out_dir / f"{stem}.speakers.txt")

    written = sorted({p.name for p in out_dir.glob(stem + ".*") if p.suffix})
    print(f"    wrote: {', '.join(written)}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def gather_inputs(raw: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in raw:
        p = Path(item)
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in MEDIA_EXTS))
        elif p.is_file():
            out.append(p)
        else:
            print(f"[skip] not found: {item}", file=sys.stderr)
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="transcribe.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", help="audio/video files or folders")
    p.add_argument("--model", help="whisper model (large-v3-turbo, large-v2, large-v3, medium, ...)")
    p.add_argument("--language", help="force language code (e.g. en); default autodetect")
    p.add_argument("--device", choices=["cuda", "cpu"])
    p.add_argument("--compute-type", dest="compute_type",
                   choices=["auto", "float16", "int8_float16", "int8", "float32"])
    p.add_argument("--batch-size", dest="batch_size", type=int)

    p.add_argument("--diarize", dest="diarize", action="store_true", default=None)
    p.add_argument("--no-diarize", dest="diarize", action="store_false")
    p.add_argument("--align", dest="align", action="store_true", default=None)
    p.add_argument("--no-align", dest="align", action="store_false")
    p.add_argument("--num-speakers", dest="num_speakers", type=int)
    p.add_argument("--min-speakers", dest="min_speakers", type=int)
    p.add_argument("--max-speakers", dest="max_speakers", type=int)

    p.add_argument("--formats", help=f"comma list or 'all' ({', '.join(ALL_FORMATS)})")
    p.add_argument("--output-dir", dest="output_dir", help="write outputs here instead of next to input")
    p.add_argument("--highlight-words", dest="highlight_words", action="store_true", default=None)
    p.add_argument("--initial-prompt", dest="initial_prompt")
    p.add_argument("--suppress-numerals", dest="suppress_numerals", action="store_true", default=None)
    p.add_argument("--beam-size", dest="beam_size", type=int)
    p.add_argument("--vad-method", dest="vad_method", choices=["pyannote", "silero"])
    p.add_argument("--overwrite", action="store_true", default=None)
    p.add_argument("--quiet", action="store_true", help="less console noise")

    p.add_argument("--emit", choices=["txt", "json", "words", "speakers"],
                   help="also print this format for the FIRST input to stdout")
    p.add_argument("--config", default=str(HERE / "config.toml"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(Path(args.config))

    # apply overrides
    for name in [
        "model", "language", "device", "compute_type", "batch_size", "align", "diarize",
        "num_speakers", "min_speakers", "max_speakers", "output_dir", "highlight_words",
        "initial_prompt", "suppress_numerals", "beam_size", "vad_method", "overwrite",
    ]:
        val = getattr(args, name)
        if val is not None:
            setattr(cfg, name, val)
    # normalise "unset" sentinels coming from config.toml
    for name in ["language", "align_model", "output_dir", "initial_prompt", "diarization_model"]:
        if getattr(cfg, name, None) == "":
            setattr(cfg, name, None)
    for name in ["num_speakers", "min_speakers", "max_speakers"]:
        if getattr(cfg, name, None) in ("", 0):
            setattr(cfg, name, None)

    if args.quiet:
        cfg.print_progress = False
    if args.formats:
        cfg.formats = list(ALL_FORMATS) if args.formats == "all" else [
            f.strip() for f in args.formats.split(",") if f.strip()
        ]
    cfg.model = MODEL_ALIASES.get(cfg.model, cfg.model)

    inputs = gather_inputs(args.inputs)
    if not inputs:
        print("no valid input files", file=sys.stderr)
        return 2

    import torch
    if cfg.device == "cuda" and not torch.cuda.is_available():
        print("[warn] CUDA not available -> using CPU", file=sys.stderr)
        cfg.device = "cpu"

    gpu_ctx = contextlib.nullcontext()
    if cfg.device == "cuda":
        sys.path.insert(0, str(HERE.parent / "gpu_lock"))
        try:
            import gpu_lock
            names = ", ".join(p.name for p in inputs[:3]) + ("..." if len(inputs) > 3 else "")
            gpu_ctx = gpu_lock.hold("Voice_to_Text", f"transcribing {names}")
        except ImportError:
            pass  # gpu_lock is optional - only useful if you have other GPU tools to share with

    import whisperx

    compute_type = resolve_compute_type(cfg)
    print(f"model={cfg.model}  device={cfg.device}  compute_type={compute_type}  "
          f"align={cfg.align}  diarize={cfg.diarize}", flush=True)

    asr_options = {
        "beam_size": cfg.beam_size,
        "temperatures": [cfg.temperature] if isinstance(cfg.temperature, (int, float))
        else list(cfg.temperature),
        "initial_prompt": cfg.initial_prompt,
        "suppress_numerals": cfg.suppress_numerals,
        "no_speech_threshold": cfg.no_speech_threshold,
        "log_prob_threshold": cfg.log_prob_threshold,
        "compression_ratio_threshold": cfg.compression_ratio_threshold,
        "condition_on_previous_text": cfg.condition_on_previous_text,
        "repetition_penalty": cfg.repetition_penalty,
        "no_repeat_ngram_size": cfg.no_repeat_ngram_size,
        "hallucination_silence_threshold": cfg.hallucination_silence_threshold,
    }
    vad_options = {
        "chunk_size": cfg.chunk_size,
        "vad_onset": cfg.vad_onset,
        "vad_offset": cfg.vad_offset,
    }

    with gpu_ctx:
        asr_model = whisperx.load_model(
            cfg.model,
            device=cfg.device,
            compute_type=compute_type,
            asr_options=asr_options,
            vad_method=cfg.vad_method,
            vad_options=vad_options,
            language=cfg.language,
            download_root=str(MODEL_DIR),
            threads=cfg.threads,
        )

        models = {
            "asr": asr_model,
            "align_cache": {},
            "diarizer": None,
            "compute_type": compute_type,
        }

        first_result = None
        failures = 0
        for path in inputs:
            out_dir = Path(cfg.output_dir) if cfg.output_dir else path.parent
            primary = out_dir / f"{path.stem}.json"
            if primary.exists() and not cfg.overwrite:
                print(f"\n=== {path.name} ===\n    skip (exists; --overwrite to redo)", flush=True)
                if first_result is None:
                    with contextlib.suppress(Exception):
                        first_result = json.loads(primary.read_text(encoding="utf-8"))
                continue
            try:
                result = transcribe_file(path, cfg, models)
                emit_outputs(path, result, cfg)
                if first_result is None:
                    first_result = result
            except Exception as exc:
                failures += 1
                import traceback
                traceback.print_exc()
                print(f"[error] {path.name}: {exc}", file=sys.stderr, flush=True)

    if args.emit and first_result is not None:
        sys.stdout.flush()
        buf = io.StringIO()
        if args.emit == "txt":
            buf.write(plain_text(first_result))
        elif args.emit == "json":
            json.dump(first_result, buf, ensure_ascii=False, indent=2)
        elif args.emit == "words":
            json.dump(flat_words(first_result), buf, ensure_ascii=False, indent=2)
        elif args.emit == "speakers":
            buf.write(speakers_text(first_result))
        print("\n" + "=" * 70 + "\n" + buf.getvalue())

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
