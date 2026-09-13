"""Shared GPU lock: a mutex for machines that run several GPU tools (a local
image/video generator, a speech-to-text pipeline, a renderer, ...) on one GPU
that can only really do one heavy job at a time.

Stdlib only (no torch/numpy/psutil), so every venv on the machine can import
this file directly by adding this folder to `sys.path` — no install step, no
shared dependency to keep in sync across tools that otherwise don't share a
Python environment.

Mutex is a lock file (`gpu.lock` next to this script) created atomically
(O_CREAT|O_EXCL). Its JSON body records who holds it and why. A lock left
behind by a dead process (crash, kill, reboot) is detected via a Windows
PID-liveness check and reclaimed automatically — no manual cleanup command to
remember.

Usage (in-process, from a sibling project in this repo):

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "gpu_lock"))
    import gpu_lock
    with gpu_lock.hold("my-tool", f"transcribing {path.name}"):
        ...actual GPU work...

Usage (wrapping an external process, e.g. a renderer launched as its own exe):

    python gpu_lock.py run --owner my-renderer --reason "scene.blend" -- ^
        C:\\Path\\To\\renderer.exe -b -P script.py

CLI: `status` (who holds it, or free), `release --force` (manual recovery if
the stale-lock auto-repair ever needs a hand), `run -- <command...>` (acquire,
run a subprocess with the lock held, release, forward its exit code).
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(HERE, "gpu.lock")

POLL_INTERVAL_S = 2.0
STATUS_EVERY_S = 20.0

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _read_lock() -> dict | None:
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _try_remove_stale(holder: dict | None) -> bool:
    """Remove the lock file if its owning PID is no longer running. Returns
    True if it removed something (caller should retry the acquire)."""
    if holder is None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(LOCK_PATH)
        return True
    if not _pid_alive(holder.get("pid", -1)):
        print(f"[gpu_lock] stale lock from dead process pid={holder.get('pid')} "
              f"({holder.get('owner')}: {holder.get('reason')}) - reclaiming", flush=True)
        with contextlib.suppress(FileNotFoundError):
            os.remove(LOCK_PATH)
        return True
    return False


def _describe(holder: dict) -> str:
    held_for = int(time.time() - holder.get("acquired", time.time()))
    return f"{holder.get('owner')} - {holder.get('reason')} (held {held_for}s, pid {holder.get('pid')})"


def acquire(owner: str, reason: str, timeout: float | None = None) -> None:
    """Block until the GPU lock is ours. Prints a status line as soon as we
    have to wait, then a heartbeat every STATUS_EVERY_S while still waiting."""
    start = time.time()
    announced = False
    last_status = 0.0
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_lock()
            if _try_remove_stale(holder):
                continue
            now = time.time()
            if not announced:
                print(f"[gpu_lock] GPU busy - waiting behind {_describe(holder)}", flush=True)
                announced = True
                last_status = now
            elif now - last_status >= STATUS_EVERY_S:
                print(f"[gpu_lock] still waiting ({int(now - start)}s) behind {_describe(holder)}", flush=True)
                last_status = now
            if timeout is not None and now - start > timeout:
                raise TimeoutError(f"gpu_lock: timed out after {timeout}s waiting behind {_describe(holder)}")
            time.sleep(POLL_INTERVAL_S)
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({
                    "pid": os.getpid(),
                    "owner": owner,
                    "reason": reason,
                    "acquired": time.time(),
                }, f)
            if announced:
                print(f"[gpu_lock] {owner} acquired the GPU after waiting {int(time.time() - start)}s "
                      f"- {reason}", flush=True)
            return


def release() -> None:
    holder = _read_lock()
    if holder is not None and holder.get("pid") != os.getpid():
        return  # not ours - never remove someone else's active lock
    with contextlib.suppress(FileNotFoundError):
        os.remove(LOCK_PATH)


@contextlib.contextmanager
def hold(owner: str, reason: str, timeout: float | None = None):
    """Context manager: acquire the GPU lock, announce it, always release."""
    acquire(owner, reason, timeout=timeout)
    print(f"[gpu_lock] {owner} holding the GPU - {reason}", flush=True)
    try:
        yield
    finally:
        release()
        print(f"[gpu_lock] {owner} released the GPU", flush=True)


def status() -> str:
    holder = _read_lock()
    if holder is None:
        return "GPU free"
    if not _pid_alive(holder.get("pid", -1)):
        return f"GPU lock present but stale (dead pid {holder.get('pid')}) - will self-clear on next acquire"
    return f"GPU held by {_describe(holder)}"


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] == "status":
        print(status())
        return 0
    if argv[0] == "release":
        force = "--force" in argv[1:]
        if not force:
            print("refusing to release without --force (this removes the lock even if something "
                  "still believes it holds the GPU)", file=sys.stderr)
            return 2
        with contextlib.suppress(FileNotFoundError):
            os.remove(LOCK_PATH)
        print("lock forcibly released")
        return 0
    if argv[0] == "run":
        rest = argv[1:]
        owner, reason = "unknown", "unspecified"
        if "--owner" in rest:
            owner = rest[rest.index("--owner") + 1]
        if "--reason" in rest:
            reason = rest[rest.index("--reason") + 1]
        if "--" not in rest:
            print("usage: gpu_lock.py run --owner NAME --reason TEXT -- <command...>", file=sys.stderr)
            return 2
        command = rest[rest.index("--") + 1:]
        if not command:
            print("no command given after --", file=sys.stderr)
            return 2
        acquire(owner, reason)
        print(f"[gpu_lock] {owner} holding the GPU - {reason}", flush=True)
        try:
            return subprocess.run(command).returncode
        finally:
            release()
            print(f"[gpu_lock] {owner} released the GPU", flush=True)
    print(f"unknown command: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
