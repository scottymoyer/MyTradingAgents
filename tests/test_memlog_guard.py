"""Unit tests for the shared-memory-log guard (repo-root ``memlog_guard.py``).

The guard exists to stop concurrent analysis threads from corrupting the single
trading-memory file via racing read-modify-write cycles. These tests exercise
the lock directly: without it the same workload loses entries (that failure is
demonstrated by the module's own __main__ self-check); with it, none are lost.
Pure logic — the lock path is redirected to a temp file so no tradingagents
import or real memory-log file is touched.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memlog_guard  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_lock(tmp_path, monkeypatch):
    """Point the guard at a temp lock file and reset its module state."""
    monkeypatch.setattr(memlog_guard, "_lock_path", tmp_path / "test.lock")
    monkeypatch.setattr(memlog_guard, "_depth", 0)
    monkeypatch.setattr(memlog_guard, "_lock_fh", None)
    return tmp_path


def test_lock_prevents_lost_updates(isolated_lock):
    """N threads each append via read-modify-write; the lock must serialize them
    so all writes survive (the exact race the guard was built to fix)."""
    data = isolated_lock / "trading_memory.md"
    data.write_text("", encoding="utf-8")

    n_threads, n_writes = 8, 25
    counter = iter(range(n_threads * n_writes))
    counter_lock = threading.Lock()

    def rmw():
        for _ in range(n_writes):
            with counter_lock:
                i = next(counter)
            with memlog_guard.memlog_lock():
                # read -> modify -> write, the pattern that loses data unguarded
                text = data.read_text(encoding="utf-8")
                data.write_text(text + f"entry-{i}\n", encoding="utf-8")

    threads = [threading.Thread(target=rmw) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [ln for ln in data.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == n_threads * n_writes
    assert len(set(lines)) == n_threads * n_writes  # every entry unique, none clobbered


def test_lock_is_reentrant(isolated_lock):
    """Guarded methods call one another, so the lock must nest without deadlock."""
    with memlog_guard.memlog_lock():
        with memlog_guard.memlog_lock():
            with memlog_guard.memlog_lock():
                depth_reached = memlog_guard._depth
    assert depth_reached == 3
    assert memlog_guard._depth == 0  # fully released


def test_lock_releases_on_exception(isolated_lock):
    """An exception inside the guarded block must still release the lock."""
    with pytest.raises(RuntimeError):
        with memlog_guard.memlog_lock():
            raise RuntimeError("boom")
    assert memlog_guard._depth == 0
    # a fresh acquisition still works (not left locked)
    with memlog_guard.memlog_lock():
        assert memlog_guard._depth == 1
