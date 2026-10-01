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
import ctypes.wintypes
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(HERE, "gpu.lock")
QUEUE_DIR = os.path.join(HERE, "gpu_queue")   # FIFO ticket queue - see _live_tickets()/acquire()

POLL_INTERVAL_S = 2.0
STATUS_EVERY_S = 20.0

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    # A successful OpenProcess() alone does NOT mean the process is still
    # running - Windows can keep the process object (and its PID) openable
    # for a while after it has exited, e.g. right after a forceful
    # TerminateProcess/Stop-Process -Force leaves a dangling handle
    # elsewhere. Confirmed reproducible: OpenProcess() returned a valid
    # handle for a PID Get-Process could no longer find at all. The actual
    # liveness signal is the exit code.
    exit_code = ctypes.wintypes.DWORD()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return bool(ok) and exit_code.value == STILL_ACTIVE


def _read_lock() -> dict | None:
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _try_remove_stale(holder: dict | None) -> bool:
    """Remove the lock file if its owning PID is no longer running. Returns
    True if it removed something (caller should retry the acquire).

    Under 3+-way contention, two waiters can independently decide the SAME
    lock is stale and race to reclaim it: one's os.remove() can land after a
    second waiter already won the race and wrote a brand-new lock file for
    itself, catching it mid-write. FileNotFoundError alone used to be
    suppressed here (the file simply being gone first), but on Windows a
    file another process still has open for writing can't be deleted at
    all - that raises PermissionError instead, which crashed the whole
    clean.py run rather than just retrying the acquire loop (confirmed
    live 2026-09-30 under 4-way gpu_lock contention: two clean.py runs
    killed mid-stem by this exact race). Suppressing it here is safe either
    way - the caller's immediate retry re-reads whatever is actually there
    now, stale or not, dead or live."""
    if holder is None:
        with contextlib.suppress(FileNotFoundError, PermissionError):
            os.remove(LOCK_PATH)
        return True
    if not _pid_alive(holder.get("pid", -1)):
        print(f"[gpu_lock] stale lock from dead process pid={holder.get('pid')} "
              f"({holder.get('owner')}: {holder.get('reason')}) - reclaiming", flush=True)
        with contextlib.suppress(FileNotFoundError, PermissionError):
            os.remove(LOCK_PATH)
        return True
    return False


def _describe(holder: dict) -> str:
    held_for = int(time.time() - holder.get("acquired", time.time()))
    return f"{holder.get('owner')} - {holder.get('reason')} (held {held_for}s, pid {holder.get('pid')})"


def _ticket_pid(name: str) -> int | None:
    """Parse the pid back out of a ticket filename ("{ts:020d}_{pid}.ticket") -
    used to prune tickets left behind by a process that died while queued
    (killed, crashed) before it ever reached the front and removed its own."""
    try:
        return int(name.rsplit(".", 1)[0].split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def _live_tickets() -> list[str]:
    """Every still-queued ticket, oldest first, with any belonging to a dead
    process pruned first (same PID-liveness check as the main lock's own
    stale-holder reclaim)."""
    try:
        names = sorted(os.listdir(QUEUE_DIR))
    except FileNotFoundError:
        return []
    live = []
    for name in names:
        pid = _ticket_pid(name)
        if pid is not None and not _pid_alive(pid):
            with contextlib.suppress(FileNotFoundError, PermissionError):
                os.remove(os.path.join(QUEUE_DIR, name))
            continue
        live.append(name)
    return live


def acquire(owner: str, reason: str, timeout: float | None = None) -> None:
    """Block until the GPU lock is ours. Prints a status line as soon as we
    have to wait, then a heartbeat every STATUS_EVERY_S while still waiting.

    Takes a FIFO queue ticket before ever touching the lock file itself, so
    under 3+-way contention a waiter is served in arrival order instead of
    racing every other waiter for the lock the instant it frees - the old
    plain os.open(O_CREAT|O_EXCL) race let a waiter that showed up LATER
    keep winning that race indefinitely against one that had been queued
    longest, starving it (confirmed live 2026-09-30: one clean.py run sat
    queued for 6+ hours while the daily sweep and the hourly no-narration
    sweep kept cutting in front of it, each new invocation of theirs re-
    winning the open() race against the same long-waiting process). Every
    acquirer takes a ticket, even on the uncontended fast path, so a
    brand-new caller can never jump a queue that already has someone in it."""
    os.makedirs(QUEUE_DIR, exist_ok=True)
    ticket_name = f"{time.time_ns():020d}_{os.getpid()}.ticket"
    ticket_path = os.path.join(QUEUE_DIR, ticket_name)
    with open(ticket_path, "w", encoding="utf-8"):
        pass
    try:
        start = time.time()
        announced = False
        last_status = 0.0
        while True:
            live = _live_tickets()
            if live and live[0] != ticket_name:
                ahead = live.index(ticket_name) if ticket_name in live else len(live)
                now = time.time()
                if not announced:
                    print(f"[gpu_lock] GPU busy - queued behind {ahead} other waiter(s)", flush=True)
                    announced = True
                    last_status = now
                elif now - last_status >= STATUS_EVERY_S:
                    print(f"[gpu_lock] still queued ({int(now - start)}s, {ahead} ahead of us)", flush=True)
                    last_status = now
                if timeout is not None and now - start > timeout:
                    raise TimeoutError(f"gpu_lock: timed out after {timeout}s queued behind {ahead} other waiter(s)")
                time.sleep(POLL_INTERVAL_S)
                continue

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
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(ticket_path)


def release() -> None:
    holder = _read_lock()
    if holder is not None and holder.get("pid") != os.getpid():
        return  # not ours - never remove someone else's active lock
    # PermissionError alongside FileNotFoundError for the same reason
    # _try_remove_stale() suppresses it: Windows can refuse a delete while
    # another process has the file open for even a moment (e.g. a waiter's
    # _read_lock() mid-read, or a racing _try_remove_stale() on a dead-
    # looking holder), even though we've just confirmed the PID in it is
    # our own. Confirmed live 2026-10-01: a release() crashed this way
    # after a file's processing was otherwise complete, losing 6.5h of
    # progress - a fresh rerun is the only recovery once that happens.
    # Safe to swallow either way: if the file truly didn't go away, our own
    # process is about to exit regardless (this only ever runs at the end
    # of a `with gpu_lock.hold(...)` block), and the next waiter's
    # _try_remove_stale() will reclaim it via the normal dead-PID path.
    with contextlib.suppress(FileNotFoundError, PermissionError):
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
