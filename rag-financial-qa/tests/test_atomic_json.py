from __future__ import annotations

import errno
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from scripts import atomic_json


def _temporary_files(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def test_non_overwrite_mode_rejects_existing_target_without_changing_it(tmp_path: Path) -> None:
    target = tmp_path / "evidence.json"
    original = {"identity": "frozen"}
    atomic_json.write_json_atomic(target, original)

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        atomic_json.write_json_atomic(target, {"identity": "replacement"}, overwrite=False)

    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert _temporary_files(target) == []


def test_concurrent_writes_leave_one_valid_payload_and_no_temps(tmp_path: Path) -> None:
    target = tmp_path / "shared.json"
    payloads = [
        {"write": index, "text": f"并发写入-{index}", "values": [index, index + 1]}
        for index in range(64)
    ]
    errors = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(atomic_json.write_json_atomic, target, payload)
            for payload in payloads
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - assertion records failures
                errors.append(exc)

    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8")) in payloads
    assert _temporary_files(target) == []


def test_replace_retries_winerror_5_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "retry.json"
    payload = {"message": "权限冲突后成功"}
    real_replace = atomic_json.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            error = PermissionError(errno.EACCES, "sharing violation")
            error.winerror = 5
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(atomic_json.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_json.time, "sleep", lambda _seconds: None)

    atomic_json.write_json_atomic(target, payload)

    assert attempts == 4
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert _temporary_files(target) == []


def test_non_retryable_replace_error_is_immediate_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "failure.json"
    attempts = 0

    def failing_replace(_source: Path, _destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise FileNotFoundError(errno.ENOENT, "not retryable")

    monkeypatch.setattr(atomic_json.os, "replace", failing_replace)

    with pytest.raises(FileNotFoundError):
        atomic_json.write_json_atomic(target, {"ok": False})

    assert attempts == 1
    assert not target.exists()
    assert _temporary_files(target) == []
