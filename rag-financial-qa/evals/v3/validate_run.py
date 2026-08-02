from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

V3_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = V3_DIR.parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from evals.v3.scoring_contract import (
    BUNDLE_ROOT_LOCATOR,
    assert_exact_score_contract,
    canonical_sha256,
    implementation_descriptor,
    load_official_bundle,
    recompute_score,
    validate_gate_b_provenance,
)

REQUIRED_STAGES = ("candidate", "generation", "score")
STAGE_SCHEMAS = {
    "candidate": "rag-answer-v3-candidate-v2",
    "generation": "rag-answer-v3-generation-v1",
    "score": "rag-answer-v3-score-v1",
}
STAGE_STATUSES = {"candidate": "frozen", "generation": "completed", "score": "official"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PREREGISTRATION = V3_DIR / "preregistration.json"
OFFICIAL_BUNDLE_ROOT = PROJECT_ROOT / BUNDLE_ROOT_LOCATOR


class V3ManifestError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_preregistration(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "rag-answer-v3-preregistration-v1":
        raise V3ManifestError("invalid preregistration schema")
    gates = payload.get("engineering_gates") or {}
    if gates.get("unknown_citation_acceptance_max") != 0:
        raise V3ManifestError("unknown citation gate must be zero")
    if gates.get("unsupported_numeric_acceptance_max") != 0:
        raise V3ManifestError("unsupported numeric gate must be zero")
    if gates.get("accepted_answer_citation_validity_min") != 1.0:
        raise V3ManifestError("citation validity gate must be one")


def _forbidden_fields(value: Any, forbidden: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in forbidden:
                found.add(str(key))
            found.update(_forbidden_fields(child, forbidden))
    elif isinstance(value, list):
        for child in value:
            found.update(_forbidden_fields(child, forbidden))
    return found


def _stage_path(run_dir: Path, identity: dict[str, Any], stage: str) -> Path:
    raw = str(identity.get("path", ""))
    path = (run_dir / raw).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise V3ManifestError(f"{stage} artifact path escapes run directory") from exc
    return path


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise V3ManifestError(f"JSON artifact must be an object: {path.name}")
    return payload


def _validate_stage_chain(
    run_dir: Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    stages = manifest.get("stages") or {}
    stage_paths: dict[str, Path] = {}
    for stage in REQUIRED_STAGES:
        identity = stages.get(stage) or {}
        path = _stage_path(run_dir, identity, stage)
        expected_sha = identity.get("file_sha256")
        if not path.is_file() or not expected_sha:
            raise V3ManifestError(f"{stage} artifact identity is incomplete")
        if not SHA256_RE.fullmatch(str(expected_sha)):
            raise V3ManifestError(f"{stage} artifact SHA malformed")
        if file_sha256(path) != expected_sha:
            raise V3ManifestError(f"{stage} artifact SHA mismatch")
        stage_paths[stage] = path

    payloads = {stage: _load_json(path) for stage, path in stage_paths.items()}
    for stage, payload in payloads.items():
        if payload.get("schema_version") != STAGE_SCHEMAS[stage]:
            raise V3ManifestError(f"invalid {stage} stage schema")
        if payload.get("status") != STAGE_STATUSES[stage]:
            raise V3ManifestError(f"invalid {stage} stage status")
        if payload.get("run_id") != manifest.get("run_id"):
            raise V3ManifestError(f"{stage} run_id mismatch")

    case_lists = [payload.get("cases") for payload in payloads.values()]
    if any(not isinstance(cases, list) for cases in case_lists):
        raise V3ManifestError("stage cases must be arrays")
    case_ids = [
        [str(case.get("case_id", "")) for case in cases]
        for cases in case_lists
    ]
    if not case_ids[0] or any(ids != case_ids[0] for ids in case_ids[1:]):
        raise V3ManifestError("stage case ID/order mismatch")
    if len(case_ids[0]) != len(set(case_ids[0])) or any(not value for value in case_ids[0]):
        raise V3ManifestError("stage case IDs must be non-empty and unique")
    expected_count = len(case_ids[0])
    if manifest.get("case_count") != expected_count or any(
        payload.get("case_count") != expected_count for payload in payloads.values()
    ):
        raise V3ManifestError("stage case count mismatch")
    return stage_paths, payloads


def validate_run(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    preregistration_path = PREREGISTRATION
    preregistration = _load_json(preregistration_path)
    validate_preregistration(preregistration)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise V3ManifestError("manifest.json is missing")
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != "rag-answer-v3-run-manifest-v2":
        raise V3ManifestError("invalid run manifest schema")
    if manifest.get("status") != "finalized" or manifest.get("immutable") is not True:
        raise V3ManifestError("run must be finalized and immutable")
    if manifest.get("run_id") != run_dir.name:
        raise V3ManifestError("manifest run_id does not match run directory")
    if manifest.get("gate_c_passed") is not True:
        raise V3ManifestError("manifest must record Gate C passed")

    contract = preregistration.get("artifact_contract") or {}
    required_identities = set(contract.get("required_identities") or []) | {
        "gate_b_official_score_file_sha256",
        "gate_b_final_manifest_file_sha256",
        "ground_truth_file_sha256",
        "ground_truth_attestation_file_sha256",
        "official_bundle_canonical_sha256",
        "gate_b_provenance_canonical_sha256",
        "implementation_canonical_sha256",
    }
    identities = manifest.get("identities") or {}
    missing_identities = sorted(required_identities - set(identities))
    if missing_identities:
        raise V3ManifestError(
            f"required identities missing: {','.join(missing_identities)}"
        )
    if any(not identities.get(name) for name in required_identities):
        raise V3ManifestError("required identity values must be non-empty")
    sha_identities = required_identities - {"model_identity", "embedding_identity"}
    malformed = sorted(
        name
        for name in sha_identities
        if not SHA256_RE.fullmatch(str(identities.get(name, "")))
    )
    if malformed:
        raise V3ManifestError(
            "required SHA identities malformed and does not match contract: "
            + ",".join(malformed)
        )

    stage_paths, payloads = _validate_stage_chain(run_dir, manifest)
    candidate = payloads["candidate"]
    generation = payloads["generation"]
    score = payloads["score"]
    if score.get("provisional") is not False:
        raise V3ManifestError("final score must not be provisional")
    if candidate.get("ground_truth_loaded") is not False:
        raise V3ManifestError("candidate stage must not load ground truth")
    if generation.get("ground_truth_loaded") is not False:
        raise V3ManifestError("generation stage must not load ground truth")
    if score.get("ground_truth_loaded") is not True:
        raise V3ManifestError("score stage must load ground truth")

    forbidden = set(
        (preregistration.get("candidate_stage") or {}).get("forbidden_fields") or []
    )
    for stage_name, payload in (("candidate", candidate), ("generation", generation)):
        leaked = sorted(_forbidden_fields(payload, forbidden))
        if leaked:
            raise V3ManifestError(
                f"{stage_name} contains forbidden fields: {','.join(leaked)}"
            )

    stage_sha_identities = {
        "candidate_file_sha256": file_sha256(stage_paths["candidate"]),
        "generation_file_sha256": file_sha256(stage_paths["generation"]),
        "score_file_sha256": file_sha256(stage_paths["score"]),
    }
    for name, actual in stage_sha_identities.items():
        if identities.get(name) != actual:
            raise V3ManifestError(f"{name} does not match stage artifact")
    if (generation.get("identities") or {}).get(
        "candidate_file_sha256"
    ) != stage_sha_identities["candidate_file_sha256"]:
        raise V3ManifestError("generation candidate_file_sha256 linkage mismatch")
    if (score.get("identities") or {}).get(
        "generation_file_sha256"
    ) != stage_sha_identities["generation_file_sha256"]:
        raise V3ManifestError("score generation_file_sha256 linkage mismatch")
    if (candidate.get("identities") or {}).get(
        "candidate_canonical_identity_sha256"
    ) != canonical_sha256(candidate["cases"]):
        raise V3ManifestError("candidate canonical identity does not match cases")

    if manifest.get("preregistration_sha256") != file_sha256(preregistration_path):
        raise V3ManifestError("preregistration SHA mismatch")
    threshold_path = preregistration_path.with_name("thresholds.json")
    if not threshold_path.is_file() or manifest.get("thresholds_sha256") != file_sha256(
        threshold_path
    ):
        raise V3ManifestError("thresholds SHA mismatch")
    thresholds = _load_json(threshold_path)

    try:
        truths, attestation, bundle = load_official_bundle(OFFICIAL_BUNDLE_ROOT)
        gate_b_provenance = validate_gate_b_provenance(run_dir, candidate)
        implementation = implementation_descriptor(PROJECT_ROOT)
        recomputed_cases, recomputed_metrics, recomputed_gate = recompute_score(
            candidate_cases=candidate["cases"],
            generation_cases=generation["cases"],
            truths=truths,
            preregistration=preregistration,
            thresholds=thresholds,
            pricing=generation.get("pricing") or {},
        )
        assert_exact_score_contract(
            score.get("cases"),
            score.get("metrics"),
            score.get("gate_c"),
            recomputed_cases,
            recomputed_metrics,
            recomputed_gate,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise V3ManifestError(f"official bundle/scoring contract failed: {exc}") from exc
    if recomputed_gate.get("passed") is not True:
        raise V3ManifestError("Gate C deterministic recomputation did not pass")

    if manifest.get("official_bundle") != bundle or score.get("official_bundle") != bundle:
        raise V3ManifestError("official bundle locator or identity does not match fixed bundle")
    if (
        candidate.get("gate_b_provenance") != gate_b_provenance
        or score.get("gate_b_provenance") != gate_b_provenance
        or manifest.get("gate_b_provenance") != gate_b_provenance
    ):
        raise V3ManifestError("Gate B provenance locator or identity does not match artifacts")
    if score.get("attestation") != attestation:
        raise V3ManifestError("score attestation does not match official bundle")
    if manifest.get("implementation") != implementation or score.get(
        "implementation"
    ) != implementation:
        raise V3ManifestError("implementation source identity does not match current source")

    candidate_identities = candidate.get("identities") or {}
    score_identities = score.get("identities") or {}
    bundle_files = bundle["files"]
    exact_identities = {
        "ground_truth_file_sha256": bundle_files["ground_truth"]["file_sha256"],
        "ground_truth_attestation_file_sha256": bundle_files["attestation"][
            "file_sha256"
        ],
        "official_bundle_canonical_sha256": bundle["canonical_sha256"],
        "gate_b_provenance_canonical_sha256": gate_b_provenance["canonical_sha256"],
        "implementation_canonical_sha256": implementation["canonical_sha256"],
        "scorer_file_sha256": implementation["sources"][
            "evals/v3/scoring_contract.py"
        ],
    }
    for name, expected in exact_identities.items():
        if identities.get(name) != expected or score_identities.get(name) != expected:
            raise V3ManifestError(f"{name} does not match fixed evidence identity")
    for name in (
        "ground_truth_file_sha256",
        "ground_truth_attestation_file_sha256",
    ):
        if candidate_identities.get(name) != exact_identities[name]:
            raise V3ManifestError(f"{name} does not match Gate B identity")
    for name in (
        "gate_b_official_score_file_sha256",
        "gate_b_final_manifest_file_sha256",
    ):
        value = candidate_identities.get(name)
        if identities.get(name) != value or not SHA256_RE.fullmatch(str(value or "")):
            raise V3ManifestError(f"{name} does not match Gate B identity")

    identity_sources = {
        "corpus_file_sha256": candidate,
        "candidate_canonical_identity_sha256": candidate,
        "ranking_sha256": candidate,
        "retrieval_config_sha256": candidate,
        "embedding_identity": candidate,
        "prompt_config_sha256": generation,
        "model_identity": generation,
    }
    for name, payload in identity_sources.items():
        stage_value = (payload.get("identities") or {}).get(name)
        if not stage_value or identities.get(name) != stage_value:
            raise V3ManifestError(f"{name} does not match stage identity")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a frozen V3 answer-quality run.")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = validate_run(args.run_dir.resolve())
    except (OSError, json.JSONDecodeError, V3ManifestError) as exc:
        print(f"[FAILED] {exc}")
        return 1
    print(
        json.dumps(
            {"status": "passed", "run_id": manifest.get("run_id")},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
