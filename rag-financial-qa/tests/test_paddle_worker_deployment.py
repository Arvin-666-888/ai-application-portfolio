from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.utils.paddle_ocr_artifact import (
    PaddleOCRArtifactError,
    portable_artifact_locator,
)
from app.workers import paddle_worker


ROOT = Path(__file__).resolve().parents[1]


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        pins[name.lower().replace("_", "-")] = version
    return pins


def test_windows_worker_requirements_combine_app_and_paddle_without_pin_conflicts():
    entrypoint = ROOT / "requirements-paddle-worker-windows-py312.txt"
    lines = [line.strip() for line in entrypoint.read_text(encoding="utf-8").splitlines()]
    assert "-r requirements.txt" in lines
    assert "-r requirements-paddleocr-windows-py312.lock.txt" in lines

    app_pins = _pins(ROOT / "requirements.txt")
    paddle_pins = _pins(ROOT / "requirements-paddleocr-windows-py312.lock.txt")
    conflicts = {
        name: (app_pins[name], paddle_pins[name])
        for name in app_pins.keys() & paddle_pins.keys()
        if app_pins[name] != paddle_pins[name]
    }
    assert conflicts == {}
    assert app_pins["numpy"] == paddle_pins["numpy"] == "2.3.5"
    assert paddle_pins["pyyaml"] == "6.0.3"


def test_portable_artifact_locator_is_relative_to_shared_root(tmp_path):
    artifact_root = tmp_path / "ocr_artifacts"
    target = artifact_root / ("a" * 12) / "p0001.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    locator = portable_artifact_locator(
        target,
        artifact_root=artifact_root,
        shared_root=tmp_path,
    )

    assert locator == "ocr_artifacts/aaaaaaaaaaaa/p0001.json"
    assert not Path(locator).is_absolute()
    assert str(tmp_path) not in locator


def test_portable_artifact_locator_rejects_path_outside_shared_root(tmp_path):
    with pytest.raises(PaddleOCRArtifactError, match="inside PADDLE_WORKER_SHARED_ROOT"):
        portable_artifact_locator(
            tmp_path.parent / "outside.json",
            artifact_root=tmp_path / "ocr_artifacts",
            shared_root=tmp_path,
        )


def _configure_windows_same_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        paddle_worker.settings, "PADDLE_WORKER_DEPLOYMENT_MODE", "windows_same_root"
    )
    monkeypatch.setattr(paddle_worker.settings, "PADDLE_WORKER_SHARED_ROOT", ".")
    monkeypatch.setattr(paddle_worker.settings, "DATABASE_URL", "sqlite:///./data/kb_qa.db")
    monkeypatch.setattr(paddle_worker.settings, "UPLOAD_DIR", "./uploads")
    monkeypatch.setattr(
        paddle_worker.settings, "DOCUMENT_PARSE_SNAPSHOT_DIR", "./parse_snapshots"
    )
    monkeypatch.setattr(paddle_worker.settings, "PDF_PADDLE_ARTIFACT_DIR", "./ocr_artifacts")
    monkeypatch.setattr(
        paddle_worker.settings,
        "PADDLE_WORKER_LOCK_FILE",
        "./requirements-paddleocr-windows-py312.lock.txt",
    )


def test_windows_same_root_contract_accepts_relative_shared_namespace(tmp_path, monkeypatch):
    _configure_windows_same_root(monkeypatch, tmp_path)

    assert paddle_worker.validate_deployment_contract(platform="win32") == tmp_path.resolve()


def test_compose_docker_only_contract_rejects_host_paddle_worker(tmp_path, monkeypatch):
    _configure_windows_same_root(monkeypatch, tmp_path)
    monkeypatch.setattr(paddle_worker.settings, "PADDLE_WORKER_DEPLOYMENT_MODE", "docker_only")

    with pytest.raises(PaddleOCRArtifactError, match="Docker API .* host Paddle worker is unsupported"):
        paddle_worker.validate_deployment_contract(platform="win32")


def test_windows_same_root_contract_rejects_container_sqlite_path(tmp_path, monkeypatch):
    _configure_windows_same_root(monkeypatch, tmp_path)
    monkeypatch.setattr(paddle_worker.settings, "DATABASE_URL", "sqlite:////app/data/kb_qa.db")

    with pytest.raises(PaddleOCRArtifactError, match="relative sqlite"):
        paddle_worker.validate_deployment_contract(platform="win32")


def test_paddle_smoke_cli_does_not_initialize_database_or_model(monkeypatch):
    fingerprint = "f" * 64
    monkeypatch.setattr(
        paddle_worker,
        "smoke_check",
        lambda device, lock_file: {
            "status": "ok",
            "configuration_fingerprint": fingerprint,
            "runtime_versions": {"paddleocr": "3.7.0"},
        },
    )
    monkeypatch.setattr(
        paddle_worker.settings, "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT", fingerprint
    )
    monkeypatch.setattr(
        paddle_worker,
        "init_db",
        lambda: pytest.fail("smoke check must not initialize the database"),
    )
    monkeypatch.setattr(
        paddle_worker,
        "create_engine",
        lambda profile: pytest.fail("smoke check must not initialize Paddle models"),
    )

    assert paddle_worker.main(["--smoke-check"]) == 0


def test_run_once_stores_portable_artifact_key(tmp_path, monkeypatch):
    artifact_root = tmp_path / "ocr_artifacts"
    target = artifact_root / ("a" * 12) / "p0001.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"status":"completed"}', encoding="utf-8")
    job = SimpleNamespace(
        id=7,
        document_id=3,
        pdf_sha256="a" * 64,
        physical_page_number=1,
        payload={"storage_path": "uploads/report.pdf", "source": "report.pdf", "reasons": []},
    )
    captured = {}

    class DB:
        def close(self):
            captured["closed"] = True

    class Heartbeat:
        lease_lost = False

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(paddle_worker, "SessionLocal", DB)
    monkeypatch.setattr(paddle_worker, "JobHeartbeat", Heartbeat)
    monkeypatch.setattr(
        paddle_worker.document_job_service, "recover_stale_jobs", lambda db: None
    )
    monkeypatch.setattr(
        paddle_worker.document_job_service, "claim_next_job", lambda *args, **kwargs: job
    )
    monkeypatch.setattr(
        paddle_worker.document_job_service,
        "complete_job",
        lambda db, **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(paddle_worker, "enqueue_finalize_if_ocr_terminal", lambda *args: True)
    monkeypatch.setattr(
        paddle_worker,
        "run_page_ocr",
        lambda **kwargs: (target, {"status": "completed", "table_count": 1}),
    )
    monkeypatch.setattr(paddle_worker.settings, "PDF_PADDLE_ARTIFACT_DIR", str(artifact_root))
    monkeypatch.setattr(paddle_worker.settings, "PADDLE_WORKER_SHARED_ROOT", str(tmp_path))

    assert paddle_worker.run_once(worker_id="worker", engine=object(), profile={}) is True
    assert captured["artifact_locator"] == "ocr_artifacts/aaaaaaaaaaaa/p0001.json"
    assert captured["artifact_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert captured["closed"] is True


def test_compose_runs_api_and_document_worker_with_shared_storage():
    """The API and document worker must share the SQLite queue and artifacts."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "rag-api:" in compose
    assert "document-worker:" in compose
    assert 'command: ["python", "-m", "app.workers.document_worker"]' in compose
    assert "DATABASE_URL: sqlite:////app/data/kb_qa.db" in compose
    assert "./data:/app/data" in compose
    assert "./uploads:/app/uploads" in compose
    assert "./chroma_data:/app/chroma_data" in compose
    assert "./parse_snapshots:/app/parse_snapshots" in compose
    assert "./ocr_artifacts:/app/ocr_artifacts" in compose
    assert "PADDLE_WORKER_DEPLOYMENT_MODE: docker_only" in compose
    assert "paddle-worker:" not in compose
