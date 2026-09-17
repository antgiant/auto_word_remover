# auto_word_remover

Three small, local-first tools that work together on one machine with one GPU:

| Folder | What it does |
|---|---|
| [`voice_to_text/`](voice_to_text/) | Local speech-to-text: WhisperX transcription + word-level alignment + speaker diarization. No cloud, no API keys. |
| [`profanity_filter/`](profanity_filter/) | Detects profanity / irreverent language in a transcript and produces a cleaned copy of the media with those spans muted, bleeped, or cut — original file untouched. Uses `voice_to_text` for transcription. |
| [`gpu_lock/`](gpu_lock/) | A tiny mutex so `voice_to_text` (and anything else you run) queues for the GPU instead of fighting other GPU-heavy tools for VRAM. |

Each has its own README and AGENTS.md with full setup/usage details and
engineering notes. This file just covers how they fit together.

## Why these three, together

`profanity_filter` calls into `voice_to_text` for transcription, and
`voice_to_text` calls into `gpu_lock` (if present) so it queues for the GPU
instead of contending with anything else on the card. Keeping them as
sibling folders in one repo means those cross-references are plain relative
paths — clone this once and they find each other, no path editing required
for the default layout:

```
auto_word_remover/
├── gpu_lock/
├── voice_to_text/
└── profanity_filter/
```

Each folder also works fine on its own if you only want one piece — `gpu_lock`
has zero dependencies on the other two, and `voice_to_text` runs standalone
if `gpu_lock` isn't present (it just won't queue for the GPU with anything
else). Only `profanity_filter` requires `voice_to_text` to be set up.

## Requirements

- Windows + PowerShell (all launchers are `.ps1`/`.cmd`; the Python itself is
  cross-platform but the setup instructions assume Windows).
- An NVIDIA GPU with CUDA for `voice_to_text` (falls back to CPU, slower).
- Python 3.11+, `ffmpeg`/`ffprobe` on PATH, and (for `profanity_filter`)
  MKVToolNix. See each project's own README for exact setup steps.

## License

MIT — see [LICENSE](LICENSE). Do what you like with it; no warranty.
