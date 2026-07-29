from pathlib import Path

import pytest

from scripts.evidence_guard import ensure_evidence_output_writable


def test_historical_canonical_is_immutable_even_with_force(tmp_path: Path) -> None:
    project = tmp_path / "project"
    target = project / "evals/task2_paddleocr/reports/retrieval_router_v1_candidates_v2.json"
    target.parent.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="historical canonical"):
        ensure_evidence_output_writable(target, project_root=project, force=True)


def test_existing_scratch_requires_force(tmp_path: Path) -> None:
    project = tmp_path / "project"
    target = project / "evals/v3/runs/scratch/candidate.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        ensure_evidence_output_writable(target, project_root=project)

    ensure_evidence_output_writable(target, project_root=project, force=True)


def test_compare_writer_cannot_overwrite_historical_result(tmp_path: Path) -> None:
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "compare_table_retrieval.py"
    spec = importlib.util.spec_from_file_location("compare_writer_guard", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    canonical = Path(module.PROJECT_ROOT) / "compare_result.json"
    with pytest.raises(FileExistsError, match="historical canonical"):
        module.write_result_atomic(canonical, {"replacement": True})


def test_finalized_v3_run_is_immutable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run_dir = project / "evals/v3/runs/run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        '{"status":"finalized","immutable":true}', encoding="utf-8"
    )

    with pytest.raises(FileExistsError, match="finalized V3 run"):
        ensure_evidence_output_writable(
            run_dir / "score.json", project_root=project, force=True
        )
