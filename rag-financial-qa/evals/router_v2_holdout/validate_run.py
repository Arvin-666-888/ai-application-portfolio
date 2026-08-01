from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class HoldoutRunValidationError(ValueError):
    pass


def _load_runner(project_root: Path):
    path = project_root / "scripts" / "run_router_v2_holdout.py"
    spec = importlib.util.spec_from_file_location("holdout_run_validator_runner", path)
    if spec is None or spec.loader is None:
        raise HoldoutRunValidationError(f"cannot load runner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise HoldoutRunValidationError(f"missing artifact: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutRunValidationError(f"invalid JSON: {path}") from exc


def _require_sha(value: Any, label: str) -> str:
    normalized = str(value or "").lower()
    if not SHA256_RE.fullmatch(normalized):
        raise HoldoutRunValidationError(f"invalid SHA-256: {label}")
    return normalized


def validate_run(root: Path, run_dir: Path) -> dict[str, Any]:
    root = root.resolve()
    run_dir = run_dir.resolve()
    if run_dir.parent != (root / "runs").resolve():
        raise HoldoutRunValidationError("run directory escapes holdout runs directory")

    project_root = Path(__file__).resolve().parents[2]
    runner = _load_runner(project_root)
    prereg_path = root / "preregistration.json"
    source_manifest_path = root / "source_manifest.json"
    prereg = _load_json(prereg_path)
    source_manifest = _load_json(source_manifest_path)
    if not isinstance(prereg, dict) or not isinstance(source_manifest, list):
        raise HoldoutRunValidationError("invalid holdout root artifacts")

    final_path = run_dir / "final_manifest.json"
    freeze_path = run_dir / "pre_gt_freeze.json"
    score_path = run_dir / "score.json"
    validation_path = run_dir / "ground_truth_validation.json"
    candidate_path = run_dir / "paired_candidates.json"
    final = _load_json(final_path)
    freeze = _load_json(freeze_path)
    score = _load_json(score_path)
    validation = _load_json(validation_path)
    candidate = _load_json(candidate_path)

    if final.get("schema_version") != "router-v2-holdout-final-manifest-v1":
        raise HoldoutRunValidationError("invalid final manifest schema")
    if prereg.get("schema_version") != "phase4-production-switch-preregistration-v1":
        raise HoldoutRunValidationError("validator only accepts Phase 4 production-switch runs")
    if runner._retrieval_profiles(prereg) != ("legacy", "financial_v3"):
        raise HoldoutRunValidationError("validator only accepts financial_v3 Gate B")
    if final.get("status") != "finalized" or final.get("immutable") is not True:
        raise HoldoutRunValidationError("run is not finalized and immutable")
    if final.get("run_id") != run_dir.name or final.get("gate_b_passed") is not True:
        raise HoldoutRunValidationError("final run identity or gate status is invalid")
    if final.get("ground_truth_loaded") is not True:
        raise HoldoutRunValidationError("final manifest must follow Ground Truth unseal")

    expected_final_inputs = {
        "preregistration_file_sha256": runner.file_sha256(prereg_path),
        "pre_gt_freeze_file_sha256": runner.file_sha256(freeze_path),
        "official_score_file_sha256": runner.file_sha256(score_path),
    }
    for name, expected in expected_final_inputs.items():
        if final.get("inputs", {}).get(name) != expected:
            raise HoldoutRunValidationError(f"final manifest link mismatch: {name}")

    if freeze.get("ground_truth_loaded") is not False or freeze.get("status") != "frozen":
        raise HoldoutRunValidationError("invalid pre-GT freeze state")
    try:
        runner._validate_frozen_root_identities(root, freeze)
    except runner.HoldoutPipelineError as exc:
        raise HoldoutRunValidationError(str(exc)) from exc
    identities = freeze.get("identities") or {}
    if identities.get("source_manifest_file_sha256") != runner.file_sha256(source_manifest_path):
        raise HoldoutRunValidationError("source manifest changed after freeze")
    for item in source_manifest:
        filename = str(item.get("filename", ""))
        pdf_path = root / "pdfs" / filename
        expected = _require_sha(item.get("sha256"), filename)
        if runner.file_sha256(pdf_path) != expected:
            raise HoldoutRunValidationError(f"source PDF changed: {filename}")
        if identities.get("source_pdf_file_sha256", {}).get(filename) != expected:
            raise HoldoutRunValidationError(f"frozen source PDF identity mismatch: {filename}")

    for stage, stage_identity in (freeze.get("stages") or {}).items():
        path = (run_dir / str(stage_identity.get("path", ""))).resolve()
        if path.parent != run_dir:
            raise HoldoutRunValidationError(f"stage path escapes run directory: {stage}")
        expected = _require_sha(stage_identity.get("file_sha256"), stage)
        if runner.file_sha256(path) != expected:
            raise HoldoutRunValidationError(f"stage artifact changed: {stage}")

    runner.validate_candidate_identity(candidate, prereg)
    current_implementation_identities = runner._implementation_identities()
    if identities.get("implementation_manifest") != current_implementation_identities:
        raise HoldoutRunValidationError("implementation manifest differs from current code")
    if candidate.get("configuration", {}).get("implementation_identities") != current_implementation_identities:
        raise HoldoutRunValidationError("candidate implementation identities are not frozen")
    if identities.get("candidate_file_sha256") != runner.file_sha256(candidate_path):
        raise HoldoutRunValidationError("candidate file changed after freeze")
    if identities.get("ranking_sha256") != candidate.get("ranking_sha256"):
        raise HoldoutRunValidationError("candidate ranking differs from freeze")
    if identities.get("candidate_canonical_sha256") != candidate.get("candidate_canonical_sha256"):
        raise HoldoutRunValidationError("candidate canonical identity differs from freeze")

    if score.get("status") != "official" or score.get("provisional") is not False:
        raise HoldoutRunValidationError("score is not official")
    if score.get("gate_b", {}).get("passed") is not True:
        raise HoldoutRunValidationError("release gate did not pass")
    if validation.get("official_score_eligible") is not True:
        raise HoldoutRunValidationError("Ground Truth validation is not official")
    score_inputs = score.get("inputs") or {}
    if score_inputs.get("candidate_file_sha256") != runner.file_sha256(candidate_path):
        raise HoldoutRunValidationError("score candidate link mismatch")
    if score_inputs.get("ground_truth_validation_file_sha256") != runner.file_sha256(validation_path):
        raise HoldoutRunValidationError("score Ground Truth validation link mismatch")

    attestation = score.get("attestation") or {}
    if not (
        attestation.get("schema_version") == "router-ground-truth-attestation-v2"
        and attestation.get("reviewer_type") == "human"
        and attestation.get("human_review_status") == "accepted"
        and attestation.get("reviewer_independence_declared") is True
        and attestation.get("ai_draft_not_used") is True
        and attestation.get("ranking_not_viewed") is True
        and attestation.get("candidate_artifacts_not_viewed") is True
    ):
        raise HoldoutRunValidationError("independent human attestation is incomplete")

    ground_truth_path = root / "private" / "ground_truth.json"
    attestation_path = root / "private" / "ground_truth_attestation.json"
    try:
        queries = runner._load_queries(root)
        official_truths, official_attestation = runner.validate_official_bundle(
            ground_truth_path=ground_truth_path,
            attestation_path=attestation_path,
            query_only_path=root / "query_only.jsonl",
            source_manifest_path=source_manifest_path,
            preregistration_path=prereg_path,
            queries=queries,
            source_manifest=source_manifest,
        )
    except runner.GroundTruthContractError as exc:
        raise HoldoutRunValidationError(f"official Ground Truth bundle is invalid: {exc}") from exc
    if score.get("attestation") != official_attestation:
        raise HoldoutRunValidationError("score attestation differs from official bundle")
    recomputed_cases, recomputed_metrics, recomputed_gate = runner.compute_scored_results(
        candidate, official_truths, source_manifest, prereg
    )
    if score.get("cases") != recomputed_cases:
        raise HoldoutRunValidationError("score cases do not match Ground Truth recomputation")
    if score.get("metrics") != recomputed_metrics:
        raise HoldoutRunValidationError("score metrics do not match Ground Truth recomputation")
    if score.get("gate_b") != recomputed_gate or recomputed_gate.get("passed") is not True:
        raise HoldoutRunValidationError("release gate does not match Ground Truth recomputation")

    profiles = runner._retrieval_profiles(prereg)
    recomputed_gate = runner.gate_b_decision(score.get("metrics") or {}, prereg)
    if recomputed_gate != score.get("gate_b") or recomputed_gate.get("passed") is not True:
        raise HoldoutRunValidationError("release gate does not match frozen thresholds")
    selected_metrics = (score.get("metrics") or {}).get(profiles[1]) or {}
    per_report = selected_metrics.get("per_report") or {}
    expected_reports = {str(item["filename"]) for item in source_manifest}
    if set(per_report) != expected_reports:
        raise HoldoutRunValidationError("per-report metrics do not cover source manifest")
    if sum(int(row.get("cases", 0)) for row in per_report.values()) != int(prereg["case_count"]):
        raise HoldoutRunValidationError("per-report case counts do not match preregistration")

    if final.get("frozen_identities") != identities or final.get("metrics") != score.get("metrics"):
        raise HoldoutRunValidationError("final manifest does not preserve frozen identities and metrics")
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a finalized sealed holdout run.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = validate_run(args.root, args.run_dir)
    except (OSError, HoldoutRunValidationError) as exc:
        print(f"[FAILED] {exc}")
        return 1
    print(json.dumps({"status": "passed", "run_id": manifest["run_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
