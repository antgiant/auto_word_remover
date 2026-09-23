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
## Reducing the Whisper miss rate

Whisper misses a lot of real profanity - not by transcribing a "cleaner"
word instead, but by never sending that audio to the decoder at all. This
was confirmed empirically, not assumed: `profanity_filter`'s
`backfill_from_srt()` already isolates exactly "words the transcript missed
entirely" (a human-authored subtitle caught them, the transcript's own word
timeline has nothing there) across this whole media library, giving a real
ground-truth dataset of 67 confirmed misses across 23 movies to test
against (2026-09-23) - not synthetic examples.

**Root cause, confirmed**: pyannote VAD (`vad_method="pyannote"`,
`vad_onset`/`vad_offset`) was rejecting the audio around a miss as "no
active speech" outright - a short, often-isolated exclamation ("Shit!",
"Damn it!") surrounded by silence or a sound-effect cue apparently doesn't
clear the default onset/offset thresholds reliably. When VAD rejects a
span, the ASR decoder never runs on it at all - no amount of `initial_prompt`
biasing, `hotwords`, or threshold tuning on the DECODER side can rescue a
span VAD never even handed it.

**What was tried, method**: extracted a ~10s clip around each of the 67
known misses from the ORIGINAL (pre-cleaning) audio track, re-transcribed
each with the model loaded once per config (`asr_options`/`vad_options`
swapped between runs without reloading the CTranslate2 model - `pipeline.
options`/`pipeline._vad_params` are plain attributes, no need to call
`load_model` again), and checked whether the target word (or same-lemma
variant, e.g. damn/damned/damnation) reappeared. A ~31-clip representative
sample was used for the exploratory sweep; the winning configuration was
then re-validated against the full 67.

| Change | Sample (31 clips) | Notes |
|---|---|---|
| baseline (current prod settings, isolated clip) | 17/31 (55%) | isolating the clip from its 2hr movie context alone already recovers over half - a real signal that 30s-chunk VAD segmentation in the FULL run is losing these, not raw acoustic difficulty |
| `temperatures=[0.0]` vs the whisperx-default fallback ladder `[0.0,0.2,0.4,0.6,0.8,1.0]` | 17/31 (identical) | confirms `temperature=0.0` was already correctly in effect (config.toml already had it) - this genuinely changes nothing, verified rather than assumed |
| `initial_prompt` (a sentence naming the target words) | 17/31 (no change) | doesn't help - consistent with VAD, not the decoder's word choice, being the gate |
| `hotwords` (faster-whisper's separate word-biasing param, confirmed actually wired through whisperx's `generate_segment_batched`, not silently ignored) | 14/31 (WORSE) | do not use for this purpose |
| `vad_method="silero"` | 14/31 (worse than pyannote) | pyannote remains the better VAD for this |
| `no_speech_threshold`/`log_prob_threshold` relaxed | 17/31 (no change) | these gate a segment AFTER VAD already accepted it - moot when VAD is the thing rejecting it |
| `model="large-v3"` (non-turbo) | 16/31, slower (34s vs 13s) | turbo isn't costing real recall here; no reason to give up its speed |
| **`vad_onset=0.3, vad_offset=0.15`** (from 0.500/0.363) | **21/31 (68%)** | biggest single lever |
| `vad_onset=0.2, vad_offset=0.1` | 20/31 | non-monotonic - more sensitive isn't strictly better partway down |
| **`vad_onset=0.1, vad_offset=0.05`** | **21/31 (68%)**, ties the 0.3/0.15 result | picked as the shipped default - see below |
| best VAD + `initial_prompt` or + relaxed no-speech-threshold | 20/31 each | confirms VAD is the gate; nothing else adds on top of it alone |
| bypassing WhisperX's VAD gate entirely (feed the whole clip as one synthetic segment straight to `generate_segment_batched`) | 0/31 | NOT a real finding - my own harness's bypass was broken (skipped tokenizer/options setup WhisperX's real `transcribe()` does), not evidence VAD-free decoding is bad. Didn't fix it given the VAD-tuning path already won; flagging here so it isn't retried as "already investigated" without re-deriving this caveat |
| vocal isolation (audio-separator, `UVR-MDX-NET-Inst_HQ_3.onnx`, `--single_stem Vocals` - the exact tool `profanity_filter/clean.py` already vendors for the opposite purpose, single_stem Instrumental) + best VAD, on the same 31 clips | **23/31 (74%)** | real additional +2 over VAD alone - confirmed genuine acoustic-masking recoveries (e.g. "Shit!" buried under an action/gunfire sound effect came back only after vocal isolation), not noise |

**Full 67-case validation** of the shipped config (`vad_onset=0.1`,
`vad_offset=0.05`) vs. the isolated-clip baseline: **42/67 (63%) recovered**
vs. 33/67 (49%) baseline - a real +9 case (+13 point) improvement from a
pure config change, no added pipeline step, no added compute cost.

**Vocal isolation was NOT wired in as a default**, despite the real
measured gain, because the cost is wildly disproportionate to the benefit:
isolating vocals for a whole feature film's audio track costs roughly the
same as `clean.py`'s own whole-track stemming pass (which was measured
taking ~80-90 minutes for a real 2+ hour 5.1 film in this same session) -
turning a ~10 minute transcription step into potentially the better part
of two hours, for +6 points of recall ON TOP of the free VAD fix, on
material that `srt_backfill`/PGS-OCR/VobSub-OCR backfill already catches a
meaningful share of anyway from a real subtitle track when one exists. If
this ever becomes worth revisiting (e.g. a much cheaper streaming
vocal-isolation model appears, or the miss rate on VAD-relaxed alone proves
too high in practice), the shape of it is: run the ORIGINAL audio through
`profanity_filter`'s `.venv-stem` audio-separator with `--single_stem
Vocals` (not `Instrumental`) before handing the result to `transcribe_file`,
matching the pattern already validated in `_run_separator`/
`build_instrumental_stem` in `clean.py` (including holding `gpu_lock` for
the duration - a real concurrent job on this machine was crashed once this
session by an unrelated wildcard cleanup interfering with its temp dir, not
by lock contention itself, which behaved correctly throughout this testing
even under heavy simultaneous GPU load from another process).

**A separate, real finding this surfaced, NOT fixable via settings**: on
several residual misses (e.g. `I, Robot (2004)`, "kiss my ass, metal
DICK" transcribed as "...metal thing!"; "SHIT" transcribed as "Stop
cussing and go home") the model appears to recognize that cursing is
happening but avoids reproducing the specific word - a real self-censorship
/ plausible-alternative bias, not a VAD or decoding-parameter problem.
No config lever tested here moved this. This is exactly the case
`backfill_from_srt`/PGS-OCR/VobSub-OCR backfill exists to catch when a real
subtitle track is available - it isn't going away by tuning Whisper harder.

**Retranscribing to benefit**: this only helps on a FUTURE transcription -
an existing cached `<name>.json` next to an already-processed file was
generated under the old VAD settings and won't improve until re-run with
`--overwrite` (or deleting the cache first). Not done automatically for any
already-processed file in this library as part of this change - re-running
the whole library is a separate, larger decision than fixing the default
going forward.

- **If you ever relocate `.venv`** (this folder was itself moved once - see repo
  history): every pip console-script `.exe` in `.venv\Scripts\` (including
  `whisperx.exe`, `pip.exe` itself, `torchrun.exe`, etc.) embeds an **absolute**
  path to that same venv's `python.exe` at install time. Moving the venv breaks
  every one of them instantly and silently (no error text, just exit code 1) -
  `python.exe script.py` invocations and `import`s are unaffected, only a
  directly-invoked `.exe` shim breaks. Fix by reinstalling each affected
  package with `--force-reinstall --no-deps` at the new location (pin exact
  versions from `uv pip freeze` first so it hits the local cache instead of
  re-downloading - this venv's own fix needed no real downloads beyond one
  small `sympy` wheel). Map broken launchers to owning packages via
  `importlib.metadata.entry_points(group="console_scripts")` rather than
  guessing from the script name.
