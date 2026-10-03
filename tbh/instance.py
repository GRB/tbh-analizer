"""One active server per data directory.

Two servers on the same database recorded every run two or three times (observed 2026-09-30).
The first server takes an exclusive OS lock on a file next to the database and keeps it for its
lifetime; others still serve the API read-only but do not collect. The OS releases the lock when
the process ends, even if it is killed.
"""
import os

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None
    import fcntl

_held = {}
LOCK_OFFSET = 4096


def acquire(data_dir):
    """True if this process now owns the collector role for `data_dir` (idempotent)."""
    path = data_dir / 'collector.lock'
    if str(path) in _held:
        return True
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(path, 'a+')
    try:
        if msvcrt:
            # Lock one byte past the PID text: a locked region cannot even be read by others on Windows.
            handle.seek(LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _held[str(path)] = handle
    return True


def owner(data_dir):
    """PID written by the process holding the lock, if readable."""
    try:
        return int((data_dir / 'collector.lock').read_text().strip() or 0) or None
    except (OSError, ValueError):
        return None
