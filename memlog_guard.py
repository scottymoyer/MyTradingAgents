#!/usr/bin/env python3
"""Serialise access to the shared trading-memory log file.

Why this exists
---------------
``TradingMemoryLog`` (tradingagents.agents.utils.memory) keeps every run's
decisions in one markdown file and mutates it with read-modify-write cycles:

    update_with_outcome / batch_update_with_outcomes
        read_text(whole file) -> rewrite -> write to <log>.tmp -> tmp.replace(log)
    store_decision
        read_text(whole file) -> append

Two hazards once analyses run concurrently:

1. Lost updates -- two workers read the same file, both rewrite it, and the
   second write silently discards the first one's changes.
2. Temp-file collision -- the temp path is ``_log_path.with_suffix(".tmp")``,
   which is *identical* for every worker, so concurrent rewrites clobber the
   same scratch file mid-flight.

Scope of the lock
-----------------
Locking is applied per *method*, not around the caller's whole workflow. Each
read-modify-write is fully contained inside a single method, so per-method
atomicity is enough to prevent lost updates -- while leaving the expensive part
(``_resolve_pending_entries`` runs LLM reflection calls *between* memory-log
calls) free to run in parallel. Locking any wider would serialise the LLM work
and defeat the point of concurrency.

The lock is re-entrant because these methods call each other
(``get_pending_entries`` -> ``load_entries``); a plain Lock would deadlock.

A ``fcntl`` file lock is taken alongside the in-process lock so that two
*separate* invocations (e.g. two ``run.sh`` runs overlapping) are also
serialised, not just threads within one process.

Nothing in site-packages is modified: this patches the class at runtime, so a
reinstall cannot silently reintroduce the race.
"""

from __future__ import annotations

import fcntl
import logging
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)

# Methods that read and/or mutate the shared log file.
_GUARDED_METHODS = (
    "store_decision",
    "load_entries",
    "get_pending_entries",
    "get_past_context",
    "update_with_outcome",
    "batch_update_with_outcomes",
)

_rlock = threading.RLock()      # re-entrant: guarded methods call one another
_depth = 0                      # nesting depth, only touched while _rlock is held
_lock_fh = None                 # open fd holding the cross-process flock
_lock_path: Path | None = None


def _resolve_lock_path() -> Path:
    """Sidecar lock file next to the memory log (never the log itself)."""
    global _lock_path
    if _lock_path is not None:
        return _lock_path
    try:
        from tradingagents.dataflows.config import get_config
        raw = get_config().get("memory_log_path")
    except Exception:
        raw = None
    if raw:
        base = Path(raw).expanduser()
    else:
        base = Path.home() / ".tradingagents" / "memory" / "trading_memory.md"
    base.parent.mkdir(parents=True, exist_ok=True)
    _lock_path = base.with_suffix(".lock")
    return _lock_path


@contextmanager
def memlog_lock():
    """Hold both the in-process and cross-process locks for the duration."""
    global _depth, _lock_fh
    with _rlock:
        _depth += 1
        outermost = _depth == 1
        if outermost:
            try:
                _lock_fh = open(_resolve_lock_path(), "a+")
                fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                # A filesystem without flock support must not break the run --
                # the in-process RLock still protects the common (threaded) case.
                logger.debug("memlog file lock unavailable (%s); thread lock only", exc)
                if _lock_fh is not None:
                    _lock_fh.close()
                _lock_fh = None
        try:
            yield
        finally:
            _depth -= 1
            if _depth == 0 and _lock_fh is not None:
                try:
                    fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_UN)
                finally:
                    _lock_fh.close()
                    _lock_fh = None


def install() -> bool:
    """Wrap TradingMemoryLog's file-touching methods with the lock.

    Idempotent; returns True when the guard is active.
    """
    from tradingagents.agents.utils import memory as _mem

    cls = _mem.TradingMemoryLog
    if getattr(cls, "_memlog_guard_installed", False):
        return True

    wrapped = []
    for name in _GUARDED_METHODS:
        original = getattr(cls, name, None)
        if original is None:
            continue

        def make(fn):
            @wraps(fn)
            def guarded(*args, **kwargs):
                with memlog_lock():
                    return fn(*args, **kwargs)
            return guarded

        setattr(cls, name, make(original))
        wrapped.append(name)

    cls._memlog_guard_installed = True
    logger.info("memory-log guard installed on: %s", ", ".join(wrapped))
    return True


if __name__ == "__main__":
    # Self-check: hammer the guarded methods from many threads and confirm the
    # shared log file is never corrupted and no entry is lost.
    import os, random, sys, tempfile, time
    logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(message)s")

    tmpdir = tempfile.mkdtemp()
    log = Path(tmpdir) / "trading_memory.md"
    log.write_text("", encoding="utf-8")
    _lock_path = log.with_suffix(".lock")

    N_THREADS, N_WRITES = 8, 25

    def unguarded_rmw(i):
        """Simulates the app's read -> modify -> write-temp -> replace cycle."""
        text = log.read_text(encoding="utf-8")
        time.sleep(random.uniform(0.0005, 0.002))   # widen the race window
        tmp = log.with_suffix(".tmp")                # SAME path for every worker
        tmp.write_text(text + f"entry-{i}\n", encoding="utf-8")
        tmp.replace(log)

    def guarded_rmw(i):
        with memlog_lock():
            unguarded_rmw(i)

    for label, fn in (("WITHOUT guard", unguarded_rmw), ("WITH guard", guarded_rmw)):
        log.write_text("", encoding="utf-8")
        threads = []
        counter = iter(range(N_THREADS * N_WRITES))
        def worker():
            for _ in range(N_WRITES):
                fn(next(counter))
        for _ in range(N_THREADS):
            t = threading.Thread(target=worker); t.start(); threads.append(t)
        for t in threads: t.join()
        got = len([l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()])
        want = N_THREADS * N_WRITES
        verdict = "OK" if got == want else f"LOST {want - got} entries"
        print(f"  {label:14}: {got}/{want} entries survived  -> {verdict}")
