"""Scratch removals a pass started but never finished: the next pass finishes them.

The prune runs on a daemon thread, so a short-lived ``hermes`` command can exit while that
thread is inside the ``rmtree`` of a large entry. The unlinks already done give the remainder's
directories fresh mtimes, so the next pass judged it freshly written and kept it for another
full idle period; with many short commands a multi-GB entry might never be reclaimed.

Each directory removal is recorded here (name, device and inode, start time) right before
``rmtree`` and the record is dropped when ``rmtree`` returns, whatever the outcome. A record
that survives means the pass died mid-delete. The next pass takes that entry without the idle
rule. Its last-moment re-check ignores directory mtimes (the removal's own) but still rescues
the entry when any file in it was written after the removal started: a writer that reused
the name keeps its work. A record whose entry is gone or whose inode changed (the name was
recreated) is dropped and the entry is judged like any other.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import hermes_constants_scratch_stuck as stuck

STATE_NAME = ".prune_inflight.json"


def _lock(root: Path):
    # Same cross-process lock as the stuck-entry records (POSIX only). Without it a lost
    # record only means that remainder waits out the idle period, as it did before.
    return stuck.state_lock(root) if stuck.tracking_supported() else contextlib.nullcontext()


def _identity(path: Path) -> list[int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [st.st_dev, st.st_ino]


def pending(root: Path) -> dict[str, float]:
    """Entries a cut-off pass was removing: name -> time that removal started."""
    if not (root / STATE_NAME).exists():
        return {}
    try:
        with _lock(root):
            state = stuck.read_state(root, STATE_NAME)
            live = {
                name: rec for name, rec in state.items()
                if isinstance(rec.get("since"), (int, float)) and rec.get("id") == _identity(root / name)
            }
            if live != state:
                stuck.write_state(root, live, STATE_NAME)
    except OSError:
        return {}
    return {name: float(rec["since"]) for name, rec in live.items()}


def begin(root: Path, entry: Path) -> None:
    ident = _identity(entry)
    if ident is None:
        return
    with contextlib.suppress(OSError), _lock(root):
        state = stuck.read_state(root, STATE_NAME)
        state[entry.name] = {"id": ident, "since": time.time()}
        stuck.write_state(root, state, STATE_NAME)


def end(root: Path, entry: Path) -> None:
    if not (root / STATE_NAME).exists():
        return
    with contextlib.suppress(OSError), _lock(root):
        state = stuck.read_state(root, STATE_NAME)
        if state.pop(entry.name, None) is not None:
            stuck.write_state(root, state, STATE_NAME)
