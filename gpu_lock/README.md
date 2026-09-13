# gpu_lock

A tiny mutex for a machine that runs more than one GPU-hungry tool and only
has one GPU to share between them. `gpu_lock.py` makes sure only one of them
actually drives the GPU at a time — everyone else waits their turn
automatically and gets told why.

- No install needed: it's a single stdlib-only file, no dependencies.
- Default behavior is to queue: if the GPU is busy, whoever asks next just
  waits and prints who they're waiting behind and for how long.
- A tool that crashed or was killed without releasing the lock doesn't jam
  things up — the next waiter detects the dead process and reclaims it
  automatically.

This repo wires it into `voice_to_text` (only while actually running on the
GPU) and, through that, `profanity_filter` (which calls into `voice_to_text`
for transcription). See `AGENTS.md` for the mechanism and for wiring in your
own tools.
