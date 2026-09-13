# Voice_to_Text

High-quality **local** speech-to-text. Send it an audio or video file, get
back plain text, full JSON, subtitles, word-level timing, and speaker labels.

Pipeline: **WhisperX** (faster-whisper / CTranslate2) transcription →
**wav2vec2** forced alignment for exact word timestamps →
token-free **pyannote 3.1** speaker diarization. Everything runs on the GPU.
No cloud, no API keys.

---

## Quick start

```powershell
.\stt.ps1 "C:\path\to\recording.m4a"
```

Outputs land **next to the input file** (`recording.txt`, `recording.json`, …).
First run with `--diarize` downloads ~30 MB of diarization models (once).

```powershell
# print just the plain text to the console
.\stt.ps1 "recording.mp3" --emit txt

# a whole folder, force 3 speakers, write everything to one place
.\stt.ps1 "C:\Recordings" --num-speakers 3 --output-dir "C:\Transcripts"

# fastest, no speaker labels
.\stt.ps1 "lecture.mp4" --no-diarize

# maximum robustness against hallucination / looping
.\stt.ps1 "hard_audio.wav" --model large-v2
```

`stt.cmd` is the same thing for `cmd.exe` — you can **drag audio/video files
onto `stt.cmd`** in Explorer, or put a shortcut to it in `shell:sendto` for
right-click *Send to → Voice_to_Text*.

Health check any time:

```powershell
.\.venv\Scripts\python.exe doctor.py
```

---

## Output files

| File | Contents |
|------|----------|
| `<name>.txt` | Plain transcript, one line per segment (with `[SPEAKER_xx]:` prefix when diarized) |
| `<name>.json` | **Full result** — every segment with `start`, `end`, `text`, `avg_logprob`, `speaker`, and a `words[]` array of `{word, start, end, score, speaker}` |
| `<name>.words.json` | Flat list of every word with timing / confidence / speaker — easiest to script against |
| `<name>.speakers.txt` | Readable transcript grouped into speaker turns with timestamps |
| `<name>.srt` | Subtitles |
| `<name>.vtt` | WebVTT captions |
| `<name>.tsv` | `start<TAB>end<TAB>text` |

Pick a subset with `--formats txt,json,srt` (or `--formats all`).

> **Profanity / "in vain" language detection and removal** lives in the
> sibling `profanity_filter` project (`flag_language.py` + `wordlists/`) — it
> scans any of these transcript formats and can also bleep the flagged spans
> out of the source media.

---

## Models

Cached under this project's own folder (`hf_cache/`, `torch_cache/`, `models/`)
by default, so a sync client (OneDrive/Dropbox/etc.) watching your user
profile can't offload multi-GB weights mid-run. Set `HF_HOME`/`TORCH_HOME`
yourself before running if you'd rather share a cache with other tools.

| Model | Path | When to use |
|-------|------|-------------|
| `large-v3-turbo` | `models\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo` | **Default.** Fast, ~large-v3 quality, lowest VRAM |
| `large-v2` | `models\models--Systran--faster-whisper-large-v2` | Most resistant to hallucination / repeat-loops |
| `large-v3` | `models\models--Systran--faster-whisper-large-v3` | Highest raw accuracy |
| wav2vec2 LARGE (LV-60k) | `models\wav2vec2_fairseq_large_lv60k_asr_ls960.pth` | English word alignment (automatic) |
| diarization (segmentation + embedding) | `hf_cache\hub\` | Speaker labels (automatic, token-free) |

Change the default in `config.toml` (`model = "..."`) or per-run with `--model`.
Any Whisper size also works on the fly: `--model medium`, `--model small.en`, …

### About v3 hallucination / looping

`large-v3` and `-turbo` historically loop and hallucinate more than `large-v2`.
This setup mitigates that heavily:

* **VAD chunking** — audio is split on silence by pyannote VAD before decoding,
  so the model never runs across long non-speech gaps (the main loop trigger).
* `condition_on_previous_text = false` — decoding can't spiral on its own output.
* `repetition_penalty = 1.15`, `no_repeat_ngram_size = 3`.
* `compression_ratio_threshold`, `log_prob_threshold`, `no_speech_threshold`
  gates drop degenerate segments.

If a specific file still misbehaves on turbo, run it again with `--model large-v2`.
All knobs are in `config.toml`.

---

## Configuration

`config.toml` holds every default. CLI flags override it. Useful ones:

| Setting / flag | Meaning |
|---|---|
| `--model` | whisper model |
| `--language en` | skip auto-detect (faster, avoids wrong-language guesses) |
| `--compute-type` | `auto` (default) picks fp16, or int8 when the GPU is busy. Force `int8_float16` to save VRAM |
| `--batch-size 4` | lower if you get CUDA out-of-memory |
| `--num-speakers` / `--min-speakers` / `--max-speakers` | constrain diarization |
| `--no-align` | skip word-level timing (segment times only) |
| `--no-diarize` | skip speaker labels (faster) |
| `--initial-prompt "..."` | bias spelling of names / jargon |
| `--highlight-words` | karaoke-style word highlighting in SRT/VTT |
| `--vad-method silero` | alternative VAD if pyannote VAD ever misbehaves |
| `--overwrite` | redo files that already have a `.json` |

---

## How it's built

* Dedicated venv: `.venv` (Python 3.11, created with [`uv`](https://github.com/astral-sh/uv)).
  Self-contained — its own venv and its own model cache, so it won't collide
  with another project's package versions.
* `torch 2.8.0+cu128`, `whisperx 3.8.6`, `faster-whisper 1.2.1`,
  `ctranslate2 4.8.2`, `pyannote.audio 4.0.7`.
* Diarization is token-free: pyannote.audio 4.x normally needs a gated
  HuggingFace model; `transcribe.py` swaps in the openly-licensed
  `tensorlake/segmentation-3.0` + `pyannote/wespeaker-voxceleb-resnet34-LM`
  with agglomerative clustering (the pyannote 3.1 recipe).

### Rebuilding the venv from scratch

```powershell
cd path\to\voice_to_text
.\bin\uv.exe venv --python 3.11 .venv
$env:UV_LINK_MODE="copy"
.\bin\uv.exe pip install torch==2.8.0+cu128 torchaudio==2.8.0+cu128 torchvision==0.23.0+cu128 --index-url https://download.pytorch.org/whl/cu128
.\bin\uv.exe pip install whisperx hf_xet soundfile
.\bin\uv.exe pip install --reinstall torch==2.8.0+cu128 torchaudio==2.8.0+cu128 torchvision==0.23.0+cu128 --index-url https://download.pytorch.org/whl/cu128
```

(the last line re-pins the CUDA build, because installing whisperx pulls the CPU wheel)

`bin\uv.exe` isn't included in this repo — grab it from
[astral-sh/uv releases](https://github.com/astral-sh/uv/releases) (or `pip
install uv`) and drop it in `bin\`, or just use `python -m venv` +
`pip install` instead of `uv` throughout if you'd rather not depend on it.

---

## GPU sharing with other tools

If you have other GPU-heavy tools on the same machine, see the sibling
`gpu_lock` project — `transcribe.py` already uses it (only while `device ==
"cuda"`; skipped entirely for CPU runs) so a transcription queues instead of
fighting another tool for VRAM. It's optional: if `../gpu_lock` isn't present,
transcription just runs without queuing.

## Notes

* A harmless `torchcodec ... libtorchcodec` warning may print — WhisperX decodes
  audio with ffmpeg directly, so it doesn't matter. Silenced by default.
* Diarization quality on very short clips or near-identical voices can be shaky;
  pass `--num-speakers N` when you know the count.
* GPU sharing: if another GPU-heavy process has the card full, `--compute-type
  auto` drops to int8 automatically. You can also just wait for it to finish.
