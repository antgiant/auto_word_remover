# Auto Word Remover

<img src="logo.png" alt="Auto Word Remover logo" width="160">

Automatically strip profanity and irreverent language out of your movies, TV
recordings, and audiobooks — without leaving dead air, without touching your
original files, and without sending a single byte anywhere. Point it at a
video or audio file; it transcribes, finds the flagged words, and hands back
a cleaned copy.

## Why this instead of a basic "bleep bot"

Most profanity filters either mute to silence or give up without a subtitle
file to work from. This one goes further:

- **Keeps the scene alive.** Instead of a dead silent gap, a flagged word is
  filled with the real ambient noise/music from that moment (audio is
  "stemmed" apart on the fly) — or, on a 5.1/7.1 mix where dialogue lives
  only on the center channel, just that channel is muted, so the score and
  effects never even blip.
- **Finds far more than the transcript alone.** Speech-to-text alone misses
  roughly a quarter of real profanity — short interjections that never make
  it to the decoder. This tool cross-checks embedded subtitles, sidecar
  `.srt` files, OCR'd Blu-ray/DVD bitmap subtitles, and can even download and
  realign a subtitle from OpenSubtitles when nothing else is available,
  closing most of that gap automatically.
- **Never touches your original media.** A new, clean audio track (and a
  matching clean subtitle track) is added and made the default; the
  untouched original stays in the file as a non-default track. Only once a
  rebuild fully succeeds does the pre-clean original move to the **Recycle
  Bin** — recoverable, never a hard delete.
- **Gives you options beyond mute.** Bleep it with a classic tone, splice it
  out entirely (genuinely lossless for mp3 sources — good for audiobooks and
  podcasts), or strip out all narration into a separate "No Narration" /
  "Wordless" track.
- **100% private.** Transcription, detection, and audio processing all run
  locally on your own GPU/CPU. Nothing leaves your device, and no account is
  required for the core pipeline.
- **You control the wordlists.** Plain text files, one word or regex pattern
  per line — edit them to your heart's content.

Removal is fully automated with no manual review step, so it isn't perfect —
it will occasionally mute a bit too much, and very rarely too little.

## What's inside

Three tools that work together, each usable on its own:

| Folder | What it does |
|---|---|
| [`profanity_filter/`](profanity_filter/) | The main event: detects profanity/irreverent language in a transcript and produces a cleaned copy of your media — muted, bleeped, or cut — with the original left untouched. |
| [`voice_to_text/`](voice_to_text/) | The transcription engine: local WhisperX speech-to-text with word-level timing and speaker diarization. Useful on its own if you just want accurate local transcripts or subtitles. |
| [`gpu_lock/`](gpu_lock/) | A tiny mutex that keeps `voice_to_text` (and anything else you run) from fighting other GPU-heavy tools for VRAM on the same machine. |

### Feature highlights

- **Multiple removal methods** — `mute` (default, smart-filled), `bleep`,
  `cut` (splice out entirely — for audio-only sources like audiobooks and
  podcasts), and `dialog` (strip *all* narration into a wordless alt track).
- **Layered subtitle-aware detection** — embedded text track → CC608 →
  sidecar `.srt`/`.vtt`/`.ass` file → OCR of Blu-ray (PGS) and DVD (VobSub)
  bitmap subtitles → OpenSubtitles download with automatic resync — each
  tried in turn to catch what the transcript alone misses.
- **Matching cleaned subtitles** — flagged words are redacted from the
  subtitle track too, right down to pixel-level redaction for bitmap
  (image-based) subtitle formats.
- **Smart reruns** — running the tool again on an already-cleaned file only
  rebuilds if the actual set of flagged words changed.
- **Audiobook/podcast support** — `cut` losslessly splices flagged spans out
  of mp3 sources (no re-encode) and remaps chapter markers onto the new,
  shorter timeline; ID3 tags and cover art are preserved.
- **Rich, standalone transcription** — full JSON/SRT/VTT/TSV output,
  word-level timestamps, and speaker labels via `voice_to_text`, usable
  independently of the profanity filter.
- **GPU-friendly** — a shared lock keeps GPU-heavy steps (transcription,
  audio stemming) from contending with each other or anything else on the
  card.
- Output almost always lands as `.mkv` (bundles video/audio/subtitles
  together with minimal reprocessing); audio-only inputs keep their own
  format instead.

## Setup difficulty — the honest version

This is a self-hosted, power-user tool, not a one-click installer. Budget
real setup time, especially on the first pass:

- **Windows only for now.** All launchers are `.ps1`/`.cmd`; the Python
  itself is cross-platform but only Windows has actually been tested.
- **Core pipeline** (mute/bleep/cut on a file with no bitmap subtitles):
  Python 3.11+, `ffmpeg`/`ffprobe` on PATH, and MKVToolNix (a portable
  download drops straight into a `bin\` folder, no installer needed). An
  NVIDIA GPU makes this practical speed-wise — it falls back to CPU but is
  noticeably slower. Realistically 15–20 minutes if you already have Python.
- **Optional extras, each adding real setup time of their own:**
  - **Ambient-noise-preserving mute / `--method dialog`** needs a second
    Python virtual environment with `audio-separator` and a matching CUDA
    build of PyTorch installed by hand (several GB of downloads). Skip it
    and muting falls back to plain silence — still fully functional, just
    less polished.
  - **Bitmap subtitle OCR** (Blu-ray/DVD) needs Tesseract OCR installed
    separately.
  - **OpenSubtitles fallback** needs a free API key from
    opensubtitles.com.
- Every optional piece degrades gracefully — a missing dependency prints a
  warning and the pipeline keeps going with what it has; it never hard-fails
  just because an extra wasn't set up.

Net honest take: it works smoothly for a straightforward stereo/mono source
with the core setup alone, and takes real additional time if you want every
fallback (surround-aware smart muting, bitmap subtitle OCR, TV-recording
subtitle matching) wired up too.

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

Each has its own README and AGENTS.md with full setup/usage details and
engineering notes. This file just covers how they fit together.

## Requirements

- Windows + PowerShell (all launchers are `.ps1`/`.cmd`; the Python itself is
  cross-platform but the setup instructions assume Windows).
- An NVIDIA GPU with CUDA for `voice_to_text` (falls back to CPU, slower).
- Python 3.11+, `ffmpeg`/`ffprobe` on PATH, and (for `profanity_filter`)
  MKVToolNix. See each project's own README for exact setup steps.

## License

MIT — see [LICENSE](LICENSE). Do what you like with it; no warranty.
