# AGENTS.md — gpu_lock

One GPU, shared by multiple tools on the same machine — `gpu_lock.py` is the
mutex that makes them take turns instead of stepping on each other's VRAM.

## Keeping the README current

When you add or materially change a user-facing feature here, update the
root [`README.md`](../README.md) (and this project's own `README.md` if
relevant) to reflect it — the root README is the marketing/feature summary
for the whole toolkit and goes stale fast otherwise.

## Layout

| Path | What |
|---|---|
| `gpu_lock.py` | the whole thing — stdlib only, no install, no venv of its own |
| `gpu.lock` | runtime state (JSON: pid/owner/reason/acquired) — created and deleted by the lock itself, not source |

## How it works

- Mutex = atomic-create of `gpu.lock` (`O_CREAT|O_EXCL`). Whoever creates it
  holds the GPU; the JSON body inside says who and why.
- `acquire(owner, reason)` blocks by default (queues) until the lock is free,
  printing who it's waiting behind as soon as it has to wait, then a
  heartbeat every 20s. A script that appears to hang because another tool has
  the GPU should never look like it's just hung.
- A lock left by a dead process (crash, kill, reboot) is detected via a
  Windows `OpenProcess` liveness check on the recorded PID and reclaimed
  automatically — no stale-lock cleanup command to remember.
- `hold(owner, reason)` is the normal API: a context manager that acquires,
  announces, and always releases (including on exception).

## Wiring in this repo

Only the code that actually drives the GPU acquires the lock; CPU-only steps
(word-list scanning, mkvmerge remuxing, ffmpeg audio encoding, JSON/report
writing) never touch it, so they never wait on anything.

| Component | Hook point | How |
|---|---|---|
| `voice_to_text/transcribe.py` | around the WhisperX model load + the transcription loop, only when `device == "cuda"` | in-process `with gpu_lock.hold(...)` |
| `profanity_filter` | `clean.py`'s `_run_separator()` (the audio-separator stemmer, `mute_fill = "stems"` / `--method dialog`) | in-process `with gpu_lock.hold(...)`, per invocation |
| `profanity_filter` (transcription) | none directly — `clean.py` / `_whisperx_check.py` shell out to `transcribe.py`, which already holds the lock for that subprocess's lifetime | inherited |

Both import this file with
`sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gpu_lock"))`
since it isn't installed into any venv — it's meant to be reached by a
relative path to this sibling folder, not pip-installed.

## Wiring in your own tools

Two shapes, depending on whether the GPU work happens in your own Python
process or in an external program you launch:

**In-process** (most common — your own script loads a model and calls it):

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gpu_lock"))
import gpu_lock

with gpu_lock.hold("my-tool", f"processing {input_name}"):
    ...load model, run inference...
```

**Wrapping an external process** (e.g. a renderer or CLI tool you invoke as
its own executable, where you can't easily inject a Python context manager
into its code):

```powershell
python gpu_lock.py run --owner my-renderer --reason "scene.blend" -- `
    C:\Path\To\renderer.exe --some --flags
```

`run` acquires the lock, runs the given command with its stdio inherited so
progress output still shows normally, releases the lock when it exits
(success or failure), and forwards its exit code.

Keep the `reason` short and specific (input filename, job id, scene name) —
it's the only thing another waiting process's user sees while queued.

## Manual recovery / inspection

```powershell
python gpu_lock.py status            # who holds it, or "GPU free"
python gpu_lock.py release --force   # only if auto-repair somehow doesn't kick in
```

Any Python 3 interpreter works (stdlib only).

## A server process (long-running, handles jobs one at a time internally)

If the GPU-using thing is a persistent server (e.g. a job queue that already
serializes its own work), the lock only needs to wrap the actual
job-execution span, not the whole server lifetime — acquire right before
running a submitted job, release right after, so the server sits unlocked
while idle and other tools can use the GPU between jobs. A no-op fallback
(skip locking if the module can't be imported) is worth adding at that call
site so a missing/relocated `gpu_lock` folder degrades to "no queuing"
instead of crashing the server.
