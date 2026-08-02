from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "03_run_paddleocr_tables.py"
)
SPEC = importlib.util.spec_from_file_location("paddleocr_batch_runner", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _job(page=3):
    return {
        "doc_id": 1,
        "source": "report.pdf",
        "source_path": Path("report.pdf"),
        "pdf_sha256": "a" * 64,
        "page_number": page,
        "reasons": ["financial_table_title"],
    }


def _profile(fingerprint="profile"):
    return {
        "configuration": {**runner.ENGINE_CONFIGURATION, "device": "gpu"},
        "locked_versions": {
            "paddleocr": "1",
            "paddlex": "1",
            "paddlepaddle-gpu": "1",
            "pymupdf": "1",
        },
        "lock_file_sha256": "lock",
        "configuration_fingerprint": fingerprint,
    }


def _completed(job, profile, tables=1):
    projected = [
        {
            "table_index": index,
            "pred_html": f"<table><td>{index}</td></table>",
            "ocr_text": str(index),
            "table_content_sha256": runner.table_content_digest(
                f"<table><td>{index}</td></table>",
                str(index),
            ),
        }
        for index in range(tables)
    ]
    return {
        "schema_version": runner.PAGE_SCHEMA,
        "status": "completed",
        "source": job["source"],
        "pdf_sha256": job["pdf_sha256"],
        "physical_page_number": job["page_number"],
        "single_page_result": {
            "page_index": 0,
            "page_count": 1,
            "page_mapping_ok": True,
        },
        "engine": profile,
        "table_count": tables,
        "tables": projected,
        "error": None,
    }


def test_runner_has_no_top_level_paddleocr_import():
    source = SCRIPT.read_text(encoding="utf-8")
    before_run_batch = source.split("def run_batch", 1)[0]

    assert "from paddleocr import" not in before_run_batch
    assert "ground-truth" not in source
    assert "expected-value" not in source


def test_projection_uses_only_true_table_list():
    payload = {
        "parsing_res_list": [{"block_label": "table", "block_content": "fake"}],
        "table_res_list": [{
            "pred_html": "<table><tr><td>营业收入</td><td>100</td></tr></table>",
            "table_ocr_pred": {"rec_texts": ["营业收入", "100"]},
        }],
    }

    tables = runner.project_tables(payload)

    assert len(tables) == 1
    assert "营业收入" in tables[0]["pred_html"]
    assert "100" in tables[0]["ocr_text"]
    assert "fake" not in tables[0]["ocr_text"]


def test_missing_table_field_is_valid_no_table_page():
    tables = runner.project_tables({"page_index": 0, "page_count": 1})

    assert tables == []


def test_non_list_table_field_is_rejected():
    with pytest.raises(runner.BatchInputError, match="不是数组"):
        runner.project_tables({"table_res_list": {"bad": True}})


def test_completed_artifact_preserves_physical_page_mapping():
    job = _job(page=113)
    artifact = runner.build_completed_artifact(
        job,
        _profile(),
        {"page_index": 0, "page_count": 1, "table_res_list": []},
        1.25,
    )

    assert artifact["physical_page_number"] == 113
    assert artifact["status"] == "completed"
    assert artifact["table_count"] == 0
    assert artifact["single_page_result"]["page_mapping_ok"] is True


def test_page_mapping_error_is_blocked():
    with pytest.raises(runner.BatchInputError, match="页码映射错误"):
        runner.build_completed_artifact(
            _job(),
            _profile(),
            {"page_index": 3, "page_count": 10, "table_res_list": []},
            1.0,
        )


def test_artifact_resume_and_stale_detection(tmp_path):
    job = _job()
    profile = _profile()
    path = runner.artifact_path(tmp_path, job)
    runner.write_json_atomic(path, _completed(job, profile))

    assert runner.classify_artifact(path, job, profile)[0] == "completed"
    assert runner.classify_artifact(path, job, _profile("changed"))[0] == "stale"

    failed = runner.build_failed_artifact(
        job,
        profile,
        RuntimeError("retry"),
        1.0,
    )
    runner.write_json_atomic(path, failed)
    assert runner.classify_artifact(path, job, profile)[0] == "failed"


def test_incomplete_completed_artifact_is_stale(tmp_path):
    job = _job()
    profile = _profile()
    path = runner.artifact_path(tmp_path, job)
    payload = _completed(job, profile)
    payload["tables"] = []
    runner.write_json_atomic(path, payload)

    assert runner.classify_artifact(path, job, profile)[0] == "stale"


def test_preflight_rejects_unexpected_json(tmp_path, monkeypatch):
    jobs = [_job()]
    raw_dir = tmp_path / "raw"
    unexpected = raw_dir / "old" / "unexpected.json"
    runner.write_json_atomic(unexpected, {"old": True})
    monkeypatch.setattr(runner, "_validate_smoke_summary", lambda path: {})
    monkeypatch.setattr(runner, "_load_jobs", lambda manifest, pdf: (jobs, "manifest"))
    monkeypatch.setattr(runner, "build_engine_profile", lambda device, lock: _profile())

    with pytest.raises(runner.StaleArtifactError, match="候选清单之外"):
        runner.validate_preflight(
            tmp_path / "candidate.json",
            tmp_path,
            tmp_path,
            tmp_path / "smoke.json",
            tmp_path / "lock.txt",
            "gpu",
        )


def test_audit_keeps_expected_denominator_and_no_table_is_completed(tmp_path):
    jobs = [_job(1), _job(2)]
    profile = _profile()
    first = runner.artifact_path(tmp_path, jobs[0])
    runner.write_json_atomic(first, _completed(jobs[0], profile, tables=0))

    summary = runner.audit_artifacts(jobs, tmp_path, profile, "manifest")

    assert summary["counts"]["expected_pages"] == 2
    assert summary["counts"]["completed_pages"] == 1
    assert summary["counts"]["missing_pages"] == 1
    assert summary["counts"]["pages_without_tables"] == 1
    assert summary["status"] == "incomplete"


def test_run_batch_reuses_success_and_retries_failed(
    tmp_path,
    monkeypatch,
):
    jobs = [_job(1), _job(2)]
    for job in jobs:
        job["source_path"] = tmp_path / "report.pdf"
    profile = _profile()
    raw_dir = tmp_path / "raw"
    runner.write_json_atomic(
        runner.artifact_path(raw_dir, jobs[0]),
        _completed(jobs[0], profile),
    )
    failed = runner.build_failed_artifact(
        jobs[1],
        profile,
        RuntimeError("retry"),
        1.0,
    )
    runner.write_json_atomic(runner.artifact_path(raw_dir, jobs[1]), failed)

    class FakeResult:
        json = {"page_index": 0, "page_count": 1, "table_res_list": []}

    class FakeEngine:
        calls = 0

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def predict(self, path):
            FakeEngine.calls += 1
            return [FakeResult()]

    monkeypatch.setitem(
        sys.modules,
        "paddleocr",
        SimpleNamespace(PPStructureV3=FakeEngine),
    )
    monkeypatch.setattr(
        runner,
        "_installed_runtime_versions",
        lambda distributions: {name: "1" for name in distributions},
    )
    monkeypatch.setattr(
        runner,
        "extract_page_as_pdf",
        lambda source, page, destination: destination.write_bytes(b"pdf"),
    )
    preflight = {
        "engine": profile,
        "jobs": jobs,
        "raw_dir": raw_dir,
        "candidate_manifest_sha256": "manifest",
    }

    summary = runner.run_batch(preflight, tmp_path, max_errors=2, device="gpu")

    assert FakeEngine.calls == 1
    assert summary["run"]["reused_completed_pages"] == 1
    assert summary["run"]["queued_this_run"] == 1
    assert summary["run"]["processed_this_run"] == 1
    assert summary["counts"]["completed_pages"] == 2
