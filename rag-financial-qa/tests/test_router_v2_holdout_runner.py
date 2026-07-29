from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_router_v2_holdout.py"
SPEC = importlib.util.spec_from_file_location("run_router_v2_holdout", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _candidate_payload() -> dict:
    cases = [
        {
            "case_id": "holdout_00",
            "question": "问题？",
            "profiles": {
                "legacy": {
                    "ranking": [{"candidate_id": "legacy-1"}],
                    "top_k": [{"candidate_id": "legacy-1"}],
                },
                "financial_v2": {
                    "ranking": [{"candidate_id": "v2-1"}],
                    "top_k": [{"candidate_id": "v2-1"}],
                    "channels": {},
                },
            },
        }
    ]
    payload = {
        "schema_version": "router-v2-holdout-paired-candidates-v2",
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "inputs": {},
        "configuration": {},
        "cases": cases,
        "ranking_sha256": runner.canonical_sha256(
            runner._candidate_ranking_identity(cases)
        ),
    }
    payload["candidate_canonical_sha256"] = runner.canonical_sha256(
        runner._candidate_canonical_identity(payload)
    )
    return payload


def _score_metrics(legacy: tuple[float, float, float], v2: tuple[float, float, float]):
    def profile(values):
        return {
            "overall": {"recall_at_5": values[0]},
            "subsets": {
                "new_company": {"recall_at_5": values[1]},
                "new_year": {"recall_at_5": values[2]},
            },
        }

    return {"legacy": profile(legacy), "financial_v2": profile(v2)}


def _prereg() -> dict:
    return {
        "release_gates": {
            "overall_row_aware_recall_at_5_min": 0.5,
            "new_company_row_aware_recall_at_5_min": 0.4,
            "new_year_row_aware_recall_at_5_min": 0.4,
            "must_not_underperform_legacy": True,
        }
    }


def test_cli_loads_project_imports_outside_project_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0
    assert "Gate B Router V2 holdout" in result.stdout


def test_dynamic_candidate_jobs_support_four_reports_and_variable_page_counts(tmp_path):
    reports = []
    expected_pages = 0
    for index, page_numbers in enumerate(([1], [2, 3], [4, 5, 6], [7])):
        source = f"report-{index}.pdf"
        path = tmp_path / "pdfs" / source
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(f"pdf-{index}".encode())
        digest = runner.file_sha256(path)
        reports.append({
            "source": source,
            "pdf_sha256": digest,
            "selected_pages": [
                {"page_number": page, "reasons": ["table"]}
                for page in page_numbers
            ],
        })
        expected_pages += len(page_numbers)

    jobs = runner._candidate_jobs(
        tmp_path,
        {"reports": reports, "selected_page_count": expected_pages},
    )

    assert len(jobs) == expected_pages
    assert len({job["source"] for job in jobs}) == 4
    assert {job["doc_id"] for job in jobs} == {1, 2, 3, 4}


def test_select_stage_is_dynamic_and_does_not_read_ground_truth(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    inventory_path = runner.artifact_path(run_dir, "inventory")
    _write(inventory_path, {
        "ground_truth_loaded": False,
        "reports": [
            {"source": f"r{index}.pdf", "sha256": str(index) * 64, "pages": [{}]}
            for index in range(1, 5)
        ],
    })
    # Its presence must not affect a pre-GT stage.
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "ground_truth.json").write_text("not-json", encoding="utf-8")
    fake_selector = SimpleNamespace(
        PDF_ROUTING_POLICY_VERSION="policy-v1",
        POLICY_FINGERPRINT="a" * 64,
        ROUTING_POLICY={"rule": "fixed"},
        select_report_pages=lambda report, max_pages: {
            "source": report["source"],
            "pdf_sha256": report["sha256"],
            "page_count": 1,
            "candidate_count_before_cap": 1,
            "selected_count": 1,
            "dropped_count": 0,
            "selected_pages": [{"page_number": 1, "reasons": ["table"]}],
            "dropped_pages": [],
        },
    )
    original_loader = runner._load_script

    def load_script(name, filename):
        if filename == "02_select_table_pages.py":
            return fake_selector
        return original_loader(name, filename)

    monkeypatch.setattr(runner, "_load_script", load_script)
    payload = runner.select_stage(tmp_path, run_dir, 9)

    assert payload["report_count"] == 4
    assert payload["selected_page_count"] == 4
    assert payload["ground_truth_loaded"] is False


def test_build_corpus_accepts_completed_artifact_without_tables():
    assert runner.SUCCESSFUL_PARSED_ARTIFACT_STATUSES == {"completed", "no_tables"}


def test_pre_gt_contract_rejects_ground_truth_fields_recursively():
    with pytest.raises(runner.HoldoutPipelineError, match="Ground Truth"):
        runner._require_pre_gt({
            "ground_truth_loaded": False,
            "cases": [{"expected_page": 10}],
        }, "candidate")

    with pytest.raises(runner.HoldoutPipelineError, match="ground_truth_loaded=false"):
        runner._require_pre_gt({"ground_truth_loaded": True}, "candidate")


def test_candidate_identity_detects_ranking_and_canonical_tampering():
    payload = _candidate_payload()
    identity = runner.validate_candidate_identity(payload)
    assert identity["ranking_sha256"] == payload["ranking_sha256"]

    payload["cases"][0]["profiles"]["legacy"]["ranking"][0]["candidate_id"] = "tampered"
    with pytest.raises(runner.HoldoutPipelineError, match="ranking identity"):
        runner.validate_candidate_identity(payload)


def test_write_new_json_refuses_overwrite(tmp_path):
    path = tmp_path / "artifact.json"
    runner.write_new_json(path, {"version": 1})

    with pytest.raises(runner.HoldoutPipelineError, match="拒绝覆盖"):
        runner.write_new_json(path, {"version": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}


def test_candidate_checks_both_outputs_before_expensive_work(tmp_path):
    root = tmp_path / "holdout"
    run_dir = root / "runs" / "r1"
    _write(run_dir / "retrieval_config.json", {"frozen": True})

    with pytest.raises(runner.HoldoutPipelineError, match="拒绝覆盖"):
        runner.candidate_stage(root, run_dir, diagnostic_k=100)


@pytest.mark.parametrize(
    ("legacy", "financial", "passed", "failed_check"),
    [
        ((0.4, 0.3, 0.4), (0.5, 0.4, 0.4), True, None),
        ((0.4, 0.3, 0.4), (0.49, 0.4, 0.4), False, "overall_min"),
        ((0.4, 0.3, 0.4), (0.5, 0.39, 0.4), False, "new_company_min"),
        ((0.4, 0.3, 0.4), (0.5, 0.4, 0.39), False, "new_year_min"),
        ((0.6, 0.5, 0.5), (0.59, 0.5, 0.5), False, "not_underperform_legacy"),
    ],
)
def test_gate_b_matches_preregistration(legacy, financial, passed, failed_check):
    result = runner.gate_b_decision(_score_metrics(legacy, financial), _prereg())

    assert result["passed"] is passed
    if failed_check:
        assert result["checks"][failed_check] is False


def test_attestation_only_accepts_all_three_official_boundaries(tmp_path):
    path = tmp_path / "ground_truth_attestation.json"
    _write(path, {
        "ranking_not_viewed": True,
        "human_review_status": "accepted",
        "reviewer_independence_declared": True,
        "draft_origin": "human-reviewed",
    })
    _, official, blockers = runner._attestation(path)
    assert official is True
    assert blockers == []

    _write(path, {
        "ranking_not_viewed": True,
        "human_review_status": "accepted",
        "reviewer_independence_declared": True,
        "draft_origin": "ai_agent_draft",
    })
    _, official, blockers = runner._attestation(path)
    assert official is False
    assert blockers == ["ai_agent_draft_not_official"]

    _write(path, {
        "ranking_not_viewed": True,
        "human_review_status": "ai_agent_draft",
        "reviewer_independence_declared": True,
    })
    _, official, blockers = runner._attestation(path)
    assert official is False
    assert blockers == ["human_review_status"]


def test_score_without_official_attestation_is_provisional_only(tmp_path, monkeypatch):
    root = tmp_path / "holdout"
    run_dir = root / "runs" / "r1"
    candidate_path = runner.artifact_path(run_dir, "candidate")
    candidate = _candidate_payload()
    candidate["cases"][0]["profiles"]["legacy"]["top_k"] = [{
        "candidate_id": "legacy-1", "content": "x"
    }]
    candidate["cases"][0]["profiles"]["financial_v2"]["top_k"] = [{
        "candidate_id": "v2-1", "content": "x"
    }]
    candidate["candidate_canonical_sha256"] = runner.canonical_sha256(
        runner._candidate_canonical_identity(candidate)
    )
    _write(candidate_path, candidate)
    freeze_path = runner.artifact_path(run_dir, "freeze-pre-gt")
    _write(freeze_path, {
        "ground_truth_loaded": False,
        "identities": {"candidate_file_sha256": runner.file_sha256(candidate_path)},
    })
    gt_path = root / "private" / "ground_truth.json"
    _write(gt_path, [{
        "case_id": "holdout_00",
        "pdf": "r.pdf",
        "question": "问题？",
        "metric": "营业收入",
        "expected_value": "100",
        "expected_page": 1,
    }])
    validation_path = runner.artifact_path(run_dir, "validate-ground-truth")
    _write(validation_path, {
        "ground_truth_loaded": True,
        "inputs": {"ground_truth_file_sha256": runner.file_sha256(gt_path)},
    })
    query_path = root / "query_only.jsonl"
    query_path.parent.mkdir(parents=True, exist_ok=True)
    query_path.write_text(
        '{"case_id":"holdout_00","question":"问题？"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(runner, "_load_queries", lambda _: [
        {"case_id": "holdout_00", "question": "问题？"}
    ])
    monkeypatch.setattr(runner, "_manifest_and_prereg", lambda _: ([], {
        "subsets": {"new_company": ["holdout_00"], "new_year": []},
        **_prereg(),
    }))
    fake_scorer = SimpleNamespace(score_case=lambda contexts, truth, scorer: {
        "hit": True, "hit_rank": 1, "miss_reason": None
    })
    original_loader = runner._load_script
    monkeypatch.setattr(
        runner,
        "_load_script",
        lambda name, filename: fake_scorer
        if filename == "compare_table_retrieval.py"
        else original_loader(name, filename),
    )

    payload = runner.score_stage(
        root,
        run_dir,
        gt_path,
        root / "private" / "ground_truth_attestation.json",
    )

    assert payload["status"] == "provisional"
    assert payload["provisional"] is True
    assert runner.artifact_path(run_dir, "score-provisional").is_file()
    assert not runner.artifact_path(run_dir, "score").exists()


def test_finalize_rejects_provisional_or_failed_gate(tmp_path):
    root = tmp_path / "holdout"
    run_dir = root / "runs" / "r1"
    _write(runner.artifact_path(run_dir, "freeze-pre-gt"), {
        "ground_truth_loaded": False,
        "identities": {},
    })
    _write(runner.artifact_path(run_dir, "score"), {
        "status": "provisional",
        "provisional": True,
    })

    with pytest.raises(runner.HoldoutPipelineError, match="score schema|official score"):
        runner.finalize_stage(root, run_dir)


def test_freeze_refuses_when_ground_truth_already_exists(tmp_path):
    root = tmp_path / "holdout"
    run_dir = root / "runs" / "r1"
    _write(root / "private" / "ground_truth.json", [])

    with pytest.raises(runner.HoldoutPipelineError, match="pre-GT freeze"):
        runner.freeze_pre_gt_stage(root, run_dir)
