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
                _lock_fh = open(_resolve_lock_path(), "a+")  # noqa: SIM115 -- held for the lock duration, closed in finally
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
