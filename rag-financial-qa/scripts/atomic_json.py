from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import Any

_MAX_REPLACE_WAIT_SECONDS = 3.0
_INITIAL_RETRY_SECONDS = 0.01
_MAX_RETRY_SECONDS = 0.25
_JITTER_SECONDS = 0.004

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def _path_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.Lock())


def _is_retryable_replace_error(exc: OSError) -> bool:
    return (
        getattr(exc, "winerror", None) in {5, 32}
        or exc.errno in {errno.EACCES, errno.EBUSY}
    )


def _retry_delay(path: Path, attempt: int) -> float:
    exponential = min(_INITIAL_RETRY_SECONDS * (2**attempt), _MAX_RETRY_SECONDS)
    seed = f"{path.resolve()}:{attempt}".encode("utf-8")
    jitter = (zlib.crc32(seed) % 1000) / 1000 * _JITTER_SECONDS
    return exponential + jitter


def _replace_with_retry(temporary: Path, path: Path) -> None:
    started = time.monotonic()
    attempt = 0
    while True:
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            if not _is_retryable_replace_error(exc):
                raise
            remaining = _MAX_REPLACE_WAIT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise
            time.sleep(min(_retry_delay(path, attempt), remaining))
            attempt += 1


def write_json_atomic(
    path: Path,
    payload: Any,
    *,
    overwrite: bool = True,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _path_lock(path):
        if not overwrite and path.exists():
            raise FileExistsError(f"refusing to overwrite existing JSON: {path}")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                _replace_with_retry(temporary, path)
            else:
                # Hard-linking publishes the fully flushed temp file only when the
                # destination does not exist, avoiding a check-then-replace race.
                os.link(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
