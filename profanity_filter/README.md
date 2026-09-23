# Profanity_Filter

Detect profanity / irreverent use of God's name in a video or audio file, and
produce a cleaned copy with those moments removed from the audio — or, as a
separate job, strip a track's dialogue entirely into a "No Narration"-style
alt track. The result **replaces the source file in place**: a new primary
(default) audio track is added, and where possible a new primary subtitle
track too, then the pre-clean original is moved to the **Windows Recycle
Bin** — recoverable, never a hard delete — once the build succeeds. Nothing
on disk is touched until then.

Two scripts, no build step:

- **`clean.py`** — the end-to-end pipeline (transcribe → flag → remove →
  remux). Reusable as a library too (`import clean`).
- **`flag_language.py`** — just the detection half, standalone: scan any
  transcript for hits and report timestamps, no media/ffmpeg involved.

Everything in this repo (`clean.py`, `flag_language.py`) is pure Python
standard library — no `pip install -r requirements.txt` needed for the tool
itself. What you do need is a handful of **external tools and one sibling
project**; see Installation.

---

## Installation

This project is meant to live as a sibling folder next to `voice_to_text` (as
it does in this repo) — `clean.py` shells out to `../voice_to_text`'s
`transcribe.py` for transcription and runs itself (via `clean.ps1`) under
that project's venv. If you rearrange the layout, update `VOICE_TO_TEXT` at
the top of `clean.py` and the `$py` path in `clean.ps1`.

### 1. Prerequisites

| Tool | Used for | Get it |
|---|---|---|
| Python 3.11+ | running the scripts | this repo runs under **voice_to_text**'s own venv (see below) — no separate Python install needed if you already have that |
| **voice_to_text** (sibling folder in this repo) | transcription (WhisperX) + the venv `clean.py` runs under + `mutagen` (ID3 tags for `--method cut` on mp3) | set up separately, see its own README |
| **ffmpeg** / **ffprobe** | every audio edit — muting, bleeping, cutting, remuxing prep | must be on PATH |
| **mkvmerge** / **mkvextract** (MKVToolNix) | subtitle extraction + final remux | bundled portably in `bin\mkvtoolnix\` (gitignored — see below) |

Python version note: `clean.py` works on Python 3.10 too (falls back to the
`tomli` pip package if the stdlib has no `tomllib`), but 3.11+ is what's
actually used day to day via voice_to_text's venv.

### 2. Get MKVToolNix into `bin\`

`bin\` is gitignored (regenerated per-machine, not shipped in the repo), so
a fresh clone needs it once:

1. Download the **portable 64-bit build** from
   [mkvtoolnix.download](https://mkvtoolnix.download/downloads.html#windows).
2. Extract it so `mkvmerge.exe` / `mkvextract.exe` end up at
   `bin\mkvtoolnix\mkvmerge.exe` (either directly in `bin\` or in
   `bin\mkvtoolnix\` both work — `clean.py` checks both).

To update later without re-downloading a zip GUI-side: grab a `.7z` build
instead and unpack it with `bin\7zr.exe x mkvtoolnix.7z -obin` (a standalone
7-Zip extractor is small enough to keep in `bin\` for exactly this — grab one
from [7-zip.org](https://www.7-zip.org/download.html) if you don't have it).

If `mkvmerge`/`mkvextract` are already installed system-wide (e.g.
`C:\Program Files\MKVToolNix\`), `clean.py` finds those too — `bin\` isn't
strictly required, just convenient for a self-contained checkout.

### 3. Set up voice_to_text

Nothing to install in this folder beyond having the sibling `voice_to_text`
project set up per its own README.

### 4. Stemmer setup (optional)

Only needed if you want `mute_fill = "stems"` (the default) or
`--method dialog`. Skip this if you're fine with `--method dialog`'s
clean-center-channel case and `mute_fill = "silence"`; everything else works
without it, degrading gracefully (a warning, or a clear error only where the
stemmer is the sole option — a >2-channel track with no clean center channel
under `--method dialog`).

`mute_fill = "stems"` (the default) and `--method dialog` use
[audio-separator](https://github.com/nomadkaraoke/python-audio-separator) to
pull ambient noise/music apart from dialogue. It lives in its **own venv**,
`.venv-stem\` next to this file — never installed into the venv `clean.py`
itself runs under (voice_to_text's) — specifically so its own dependency pins
(torch/onnxruntime) can never touch the versions anything else you run
depends on. One-time setup, not automatic:

```powershell
python -m venv .venv-stem
.venv-stem\Scripts\pip install audio-separator[gpu]

# `pip install torch` on Windows defaults to a CPU-only build even inside the
# [gpu] extra - audio-separator gates ALL GPU use (onnxruntime included) on
# torch.cuda.is_available(), so without this next step it silently runs on
# CPU. Install a matching CUDA build of torch + torchvision explicitly (check
# https://download.pytorch.org/whl/torch/ and .../torchvision/ for the latest
# matching pair for your GPU/driver):
.venv-stem\Scripts\pip install `
  "torch==2.6.0+cu124" "torchvision==0.21.0+cu124" `
  --index-url https://download.pytorch.org/whl/cu124

# onnxruntime-gpu also needs the CUDA/cuDNN runtime DLLs importable - these
# pip packages supply them (self-contained in .venv-stem, ~1.6 GB):
.venv-stem\Scripts\pip install `
  nvidia-cudnn-cu12 nvidia-cublas-cu12 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12

# no CUDA GPU: skip both of the above and use audio-separator[cpu] instead -
# it'll work, just slower on CPU than with the GPU steps above.
```

Verify it actually picked up the GPU (look for "CUDA is available in Torch" /
"ONNXruntime has CUDAExecutionProvider available" in a run's output — if you
instead see "No hardware acceleration could be configured, running in CPU
mode", the torch build is still CPU-only):

```powershell
.venv-stem\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"
# should print True
```

The default model (`UVR-MDX-NET-Inst_HQ_3.onnx`) downloads automatically on
first use and is cached under the venv for later runs.

A >2-channel track with no clean center channel is the slow case for
`--method dialog` — the stemmer runs once *per channel* (see
`build_instrumental_stem`) to keep the output at the source's channel count,
so a 5.1 track means 6 separate passes over the full runtime. See
[WhisperX validation](#whisperx-validation-experimental) below for a way to
cut down how often that slow path actually triggers.

### Install checklist

- [ ] `voice_to_text` set up as a sibling folder (its own venv, its own README)
- [ ] `ffmpeg` / `ffprobe` on PATH
- [ ] `bin\mkvtoolnix\mkvmerge.exe` present (or MKVToolNix installed system-wide)
- [ ] *(optional)* `.venv-stem\` set up per step 4, GPU verified if you have one

---

## Usage

### Quick start

```powershell
# clean a movie (transcribes it first if there's no .json next to it)
.\clean.ps1 "C:\Media\Movie (2002).mkv"

# see what would be removed, do nothing
.\clean.ps1 "Movie.mkv" --dry-run
```

The result replaces `Movie.mkv` itself (original moved to the Recycle Bin),
plus a `Movie.bleeps.json` sidecar next to it (exactly what was removed).
`out\` is scratch space only — temp files during the run, and `--keep-temp`
debug artifacts.

`clean.ps1` is a thin wrapper that runs `clean.py` under voice_to_text's venv
with UTF-8 console output forced; calling `clean.py` directly works the same
way as long as you invoke it with a Python that has that venv's packages
(or, for `--method mute`/`--method dialog` without transcription/`cut`
needs, any Python 3.10+ — see the version note above).

### What it does

1. **Transcript** — reuses `<name>.json` beside the input, otherwise runs
   voice_to_text (`--formats json --no-diarize`). Skipped entirely for
   `--method dialog`, which doesn't consult the wordlists at all.
2. **Flag** — `flag_language.py` finds every profanity hit with a word-accurate
   timestamp, then cross-checks the media's embedded SubRip track (if any) for
   words the transcript missed entirely — Whisper drops or mis-hears a lot of
   short interjections. Missed words are timed via the transcript's own word
   timings where they line up, interpolated where the word was dropped
   outright. On a test film this roughly doubled the hits found (25 -> 46).
   Disable with `--no-srt-backfill`.
3. **Remove** — one `ffmpeg` pass over a single audio track:
   - `--method mute` **(default)** — silence. If the track has more than two
     channels, the center channel's actual content is measured first: when
     dialogue really is isolated there, **only the center channel is muted** —
     music/effects on every other channel play through completely unbroken.
     Otherwise (stereo, or dialogue isn't center-only), the muted span isn't
     dead air either by default: a stemmer pulls the ambient noise/music out
     of the whole track first, and that plays through the mute instead
     (`mute_fill = "stems"`, needs the [stemmer setup](#4-stemmer-setup-optional)
     above; `mute_fill = "silence"` for the old dead-air behavior). See
     AGENTS.md for how the center-channel detection works.
   - `--method bleep` — every channel is muted and a 1 kHz tone laid over it.
   - `--method cut` — the flagged span is **spliced out entirely**, shortening
     the file. **Audio-only inputs only** (audiobooks, podcasts, ...) — refuses
     any file with a real video track (embedded cover art is fine) since
     cutting a video's audio would desync it from the picture. **On an mp3
     source this is genuinely lossless** — every kept region is a plain
     stream copy, bit-identical to the source; nothing is decoded or
     re-encoded, only the flagged spans disappear. Every other codec falls
     back to a decode+re-encode pass. ID3v2 tags (title/artist/album/cover
     art/custom frames — everything) are copied onto an mp3 output
     byte-for-byte via `mutagen`, not ffmpeg's lossier `-map_metadata`.
   - `--method dialog` — a different job: strip **all** dialogue (not just
     flagged words — the wordlists/transcript aren't even consulted) into a
     new `(Wordless)` track, added alongside the original as a **non-default**
     alt track (`--dialog-default` / `dialog_track_default = true` to make it
     default instead). Same center-channel-first logic as `mute`, gated on
     subtitle-confirmed dialogue moments (not diluted by the whole runtime —
     see AGENTS.md); a clean center channel gets muted alone for the entire
     runtime (channel count trivially preserved). Otherwise the
     [stemmer](#4-stemmer-setup-optional)
     strips dialogue from every channel and that becomes the output, keeping
     the source's channel count wherever the stemmer can manage it
     (stereo/mono directly; >2 channels by stemming each channel separately
     and rejoining — see `build_instrumental_stem`). Needs the stemmer for
     anything without a clean center channel; errors out clearly if it's not
     installed for that case.

   `mute`/`bleep`/`dialog` encode to a standalone track (AC-3 by default —
   224k for a stereo source, 448k for a multichannel one; `flac` for lossless
   instead) — the *only* re-encoding — then get muxed back in (step 5). `cut`
   on a non-mp3 source re-encodes with a codec matching the source's own —
   no remux, no alt track (the duration changed, so keeping the original
   alongside doesn't make sense).
4. **Subtitles** (`mute`/`bleep` only) — the embedded SubRip track is pulled
   with `mkvextract`; every cue that overlaps a removed span gets its profane
   words replaced with `***`. `--method dialog` leaves subtitles completely
   alone.
5. **Remux** — `mkvmerge` copies the original bit-for-bit and adds the
   cleaned audio (**and** cleaned subtitle, for `mute`/`bleep`) back in.
   `mute`/`bleep` add it as a new **default** track called
   `<original label> (Cleaned)`, demoting the original. `dialog` adds
   `<original label> (Wordless)` as a **non-default** track by default
   (`--dialog-default` to flip that). Video and every other track are
   untouched either way.
6. **Replace** — all of the above is built in a temp dir first, never
   touching the source. Only once it succeeds: the pre-clean original is
   moved to the **Recycle Bin** and the newly built file takes its place at
   the source's own path (same folder/stem — the extension only changes for
   `mute`/`bleep`/`dialog` on a non-`.mkv` source, since mkvmerge always
   writes `.mkv`; `cut` always keeps the source's own extension). If nothing
   was flagged, or `--dry-run` is passed, the source is never touched at all.

### Useful options

| Flag | Meaning |
|---|---|
| `--method mute\|bleep\|cut\|dialog` | removal method (default `mute`; `cut` is audio-only-input only; `dialog` strips ALL dialogue, ignores the wordlists) |
| `--dry-run` | print the spans and stop |
| `--pad 0.15` | padding (s) before & after each word (`--pad-start` / `--pad-end` for asymmetry) |
| `--categories profanity,irreverence` | which lists to act on (default `profanity`) — **quote this value** (`--categories "profanity,irreverence"`); PowerShell mangles an unquoted comma into a space before it reaches `clean.py`, silently emptying the matcher list (see AGENTS.md) |
| `--center-margin-db` | `mute`/`dialog`: how many dB louder the center channel must be than every other channel to mute it alone (default 6) |
| `--mute-fill stems\|silence` | `mute` only: fill a center-less muted span with the stemmed-out ambient noise/music (default) or dead silence |
| `--stem-model` | audio-separator model for stemming (default `UVR-MDX-NET-Inst_HQ_3.onnx`) |
| `--dialog-default` | `dialog` only: make the new `(Wordless)` track the default audio track |
| `--beep-hz` / `--beep-gain-db` | `bleep` only: tone frequency / level (default 1000 Hz, −6 dBFS) |
| `--clean-codec ac3\|eac3\|aac\|flac` / `--clean-bitrate` / `--clean-bitrate-surround` | `mute`/`bleep`/`dialog` only: codec + bitrate for the cleaned track (default `ac3` @ 224k for <=2ch, 448k for >2ch; or use `flac` for lossless) |
| `--cut-bitrate` | `cut` only: bitrate for a lossy source codec (default 96k) |
| `--source-track default\|0\|1\|eng` | which audio track to clean |
| `--subs-track default\|0\|eng\|none` | `mute`/`bleep` only: which SubRip track to clean (`--no-subs` to skip) |
| `--sync-ms N` | `mute`/`bleep`/`dialog` only: delay the clean track by N ms if lip-sync drifts |
| `--extra-spans file.json` | hand-reviewed `[{start,end,label,category}]` spans to remove in addition to the wordlists (e.g. content no regex can safely catch) — always included, regardless of `--categories` |
| `--keep-temp` | also drop the cleaned track + ffmpeg filter graph in `out\` |
| `--output-dir DIR` | scratch dir for temp files (default `out\`) — not where the result ends up; that always replaces the source |
| `--overwrite` | allow clobbering a leftover file at the destination from an earlier run where the extension changed; irrelevant when the destination is the source's own path (always replaced) |

Run `clean.ps1 --help` (or `clean.py --help`) for the full list, including
`--retranscribe`, `--no-srt-backfill`, and `--config` (point at a different
`config.toml`).

### A note on `--method cut`

Audiobooks and podcasts don't have a picture to desync and aren't usually
switched between "clean"/"explicit" via track selection the way movies are —
so for those, cutting the word out entirely (rather than leaving a silent or
bleeped gap) reads as the more natural result. It fundamentally can't apply to
video: cutting time out of the audio track alone would drift it out of sync
with the picture immediately. Chapter markers are **remapped** onto the new,
shorter timeline (not dropped) — a chapter entirely inside a removed span is
the one exception, since there's nothing left of it to keep.

All defaults live in `config.toml` — copy it and pass `--config` to keep a
project-specific set without touching the shared one.

---

## flag_language.py (standalone)

Scan transcripts without touching media. Reads `.json`, `.words.json`, `.srt`,
`.vtt`, `.lrc`, `.tsv`, `.speakers.txt`, `.txt`.

```powershell
$py = "..\voice_to_text\.venv\Scripts\python.exe"
& $py flag_language.py "meeting.srt"
& $py flag_language.py "C:\Transcripts" --recurse --report flags.txt
& $py flag_language.py "sermon.json" --only irreverence
```

Writes `<input>.flags.json` next to each file; exit code `1` when anything is
flagged (`--fail-on none` to disable). When scanning a `.json`/`.words.json`
that has a sibling `<same name>.srt`, it's automatically cross-checked for
words the transcript missed (`--no-srt-backfill` to disable) — see
`flag_language.backfill_from_srt()` in AGENTS.md for how.

### Word lists

`wordlists\profanity.txt` and `wordlists\irreverence.txt` are plain text, one
entry per line — a literal phrase (whole-word, case-insensitive) or `re:<regex>`;
`#` starts a comment. They're re-read on every run, so tune them freely.

`irreverence.txt` has only the high-precision expletive patterns active by
default ("goddamn", "oh my God", "Jesus Christ", "for heaven's sake", minced
oaths). Uncomment the block at the bottom to also flag every bare
"God" / "Jesus" / "Lord" / "Christ" for manual review.

---

## WhisperX validation (experimental)

`_whisperx_check.py` answers a sharper question than the dB-margin test above
can: is a center-channel mute actually **inaudible**, not just quieter on
average? It picks the longest subtitle-confirmed dialogue spans in a file,
transcribes the real audio and a center-muted version of the same spans with
WhisperX (via voice_to_text), and compares. A clean mute leaves only short
generic hallucinated phrases ("Thank you.", "Oh, God.") with near-zero word
overlap against the real (coherent, on-topic) transcript; real bleed-through
shows up as an actual matching sentence fragment.

Not wired into `clean.py` as a flag yet — it's a standalone script, since it
needs WhisperX and costs a few short transcriptions per file (real, but much
cheaper than a stemming pass). Useful when `--center-margin-db`'s default (or
even a lowered one) is keeping `--method dialog` on the slow stemmer path for
a source where the center channel is probably fine — see AGENTS.md
("WhisperX validation") for how it changed the real-world call on a batch of
episodic content.

```powershell
$py = "..\voice_to_text\.venv\Scripts\python.exe"
& $py _whisperx_check.py "Movie.mkv" --audio-pos 0 --center-idx 2 --layout "5.1(side)"
```

---

## Project layout

```
clean.py             the pipeline: transcript -> flag -> remove -> remux
flag_language.py     standalone detection (no media/ffmpeg needed)
_whisperx_check.py   experimental: validate a center-mute with real ASR
config.toml          defaults for clean.py (copy + --config to override)
wordlists\           profanity.txt / irreverence.txt - edit freely
bin\                 gitignored - mkvmerge/mkvextract go here (see Install)
.venv-stem\          gitignored - optional stemmer venv (see Install step 4)
out\                 scratch dir (temp files + --keep-temp debug artifacts) -
                     the cleaned result replaces the source in place instead
AGENTS.md            engineering notes: how things work, known gotchas,
                     design rationale - read this before changing behavior
```

## Requirements summary

- **voice_to_text** as a sibling folder (venv + `transcribe.py`). Its venv
  also supplies `mutagen`, used by `--method cut` on an mp3 source to copy
  ID3v2 tags (title/artist/cover art/everything) onto the cleaned file.
- **ffmpeg / ffprobe** on PATH.
- **mkvmerge** — bundled at `bin\mkvtoolnix\`. Update by unpacking a newer
  portable `.7z` from mkvtoolnix.download with `bin\7zr.exe`.
- **audio-separator**, in its own `.venv-stem\` (several GB with the GPU
  packages) — only needed for `mute_fill = "stems"` (the default) and
  `--method dialog`; see [Stemmer setup](#4-stemmer-setup-optional).
  Both degrade gracefully without it.
