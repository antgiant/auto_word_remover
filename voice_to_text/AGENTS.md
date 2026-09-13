# AGENTS.md — Voice_to_Text

Local, GPU-accelerated speech-to-text. Read this before changing anything
here. `README.md` has the full user-facing docs; this file is the operator's
cheat-sheet.

## Layout

| Path | What |
|---|---|
| `transcribe.py` | the pipeline: WhisperX → wav2vec2 alignment → token-free pyannote 3.1 diarization |
| `config.toml` | every default; CLI flags override it |
| `stt.ps1` / `stt.cmd` | launchers, run the venv |
| `doctor.py` | health check — run after any dependency or asset change |
| `.venv/` | dedicated Python 3.11 venv (`uv`) |
| `models/` | whisper + wav2vec2 weights |
| `hf_cache/`, `torch_cache/` | self-contained HuggingFace/torch caches (see README "Models") |

This folder is **transcription only**. Profanity / "in vain" language
detection and removal lives in the sibling `profanity_filter` project
(`flag_language.py` + `wordlists/`), which calls this tool when it needs a
transcript.

## Running

```powershell
.\stt.ps1 "C:\path\recording.m4a"          # outputs land next to the input
.\stt.ps1 "C:\Recordings" --num-speakers 3 --output-dir "C:\Transcripts"
.\.venv\Scripts\python.exe doctor.py
```

Outputs per input: `.txt .json .srt .vtt .tsv .words.json .speakers.txt`
(`.json` is the source of truth — full segments + per-word `start/end/score/speaker`).

## Conventions

- **GPU lock**: if you have other GPU-heavy tools on the same machine, drop
  this repo's sibling `gpu_lock` project next to this one. When
  `cfg.device == "cuda"`, `main()` in `transcribe.py` acquires it
  (`gpu_lock.hold("Voice_to_Text", ...)`) around the model load + transcription
  loop, so a run queues automatically if something else already has the GPU
  (and prints why it's waiting). CPU-only runs never touch the lock, and the
  import is wrapped in `try/except ImportError` so this all degrades to "no
  queuing" if `gpu_lock` isn't present. See `../gpu_lock/AGENTS.md`.
  `profanity_filter`'s `clean.py` and `_whisperx_check.py` inherit this for
  free since they invoke `transcribe.py` as a subprocess.
- Model/cache paths are self-contained under this project's own folder by
  default (`HF_HOME`, `TORCH_HOME` set via `os.environ.setdefault(...)` in
  `transcribe.py`) so a sync client watching your user profile can't offload
  them mid-run. Override by setting the env var yourself before running.
- `profanity_filter` reuses **this** venv rather than building its own — keep
  its dependencies (`mutagen`, etc.) compatible if you add packages here.
- New Python here targets 3.11, uses `from __future__ import annotations`, and
  keeps heavy imports (`torch`, `whisperx`) inside functions so `--help` and
  `doctor.py` stay fast.
- After changing deps or moving assets, run `doctor.py` and paste the result.
