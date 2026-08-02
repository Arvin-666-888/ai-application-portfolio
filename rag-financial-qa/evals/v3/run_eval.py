from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

V3_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = V3_DIR.parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.services.answer_verification_service import build_citation_ledger
from app.services.rag_service import execute_answer_from_contexts
from evals.v3.scoring_contract import (
    BUNDLE_ROOT_LOCATOR,
    GATE_B_ARTIFACT_LOCATORS,
    PROFILES,
    assert_exact_score_contract,
    canonical_sha256,
    implementation_descriptor,
    load_official_bundle,
    recompute_score,
    score_output as contract_score_output,
    validate_gate_b_provenance,
)

PREREGISTRATION = V3_DIR / "preregistration.json"
THRESHOLDS = V3_DIR / "thresholds.json"
OFFICIAL_BUNDLE_ROOT = PROJECT_ROOT / BUNDLE_ROOT_LOCATOR
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
REFUSAL_TERMS = ("无法回答", "无法可靠回答", "资料不足")


class V3EvalError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_new_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise V3EvalError(f"拒绝覆盖已有 artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_dir(runs_root: Path, run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise V3EvalError("run_id 必须是单一路径名称")
    return runs_root.resolve() / run_id


def _assert_mutable(run_dir: Path) -> None:
    manifest = run_dir / "manifest.json"
    if manifest.exists():
        payload = load_json(manifest)
        if payload.get("status") == "finalized" or payload.get("immutable") is True:
            raise V3EvalError("finalized run immutable，拒绝覆盖")


def _cases(payload: Any) -> list[dict[str, Any]]:
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise V3EvalError("cases 必须是非空数组")
    return cases


def _unique_ids(cases: list[dict[str, Any]]) -> list[str]:
    ids = [str(case.get("case_id") or case.get("id") or "") for case in cases]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise V3EvalError("case_id 必须非空且唯一")
    return ids


def _candidate_contexts(case: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    profiles = case.get("profiles") or {}
    selected = profiles.get(profile)
    if not isinstance(selected, dict):
        raise V3EvalError(f"Gate B candidate 缺少 profile: {profile}")
    contexts = selected.get("top_k")
    if not isinstance(contexts, list):
        raise V3EvalError(f"Gate B candidate {profile}.top_k 无效")
    return [dict(item) for item in contexts]


def _validate_gate_b_release(
    paired_path: Path, freeze_path: Path, score_path: Path, manifest_path: Path
) -> dict[str, str]:
    if not score_path.is_file() or not manifest_path.is_file():
        raise V3EvalError("V3 candidate 要求 Gate B official score 和 final manifest")
    score = load_json(score_path)
    manifest = load_json(manifest_path)
    if score.get("schema_version") != "router-v2-holdout-score-v1":
        raise V3EvalError("Gate B score schema 无效")
    if score.get("status") != "official" or score.get("provisional") is not False:
        raise V3EvalError("Gate B score 必须是 official")
    if score.get("gate_b", {}).get("passed") is not True:
        raise V3EvalError("Gate B 未通过")
    if (
        manifest.get("schema_version") != "router-v2-holdout-final-manifest-v1"
        or manifest.get("status") != "finalized"
        or manifest.get("immutable") is not True
        or manifest.get("gate_b_passed") is not True
    ):
        raise V3EvalError("Gate B final manifest 无效")
    inputs = manifest.get("inputs") or {}
    if inputs.get("official_score_file_sha256") != file_sha256(score_path):
        raise V3EvalError("Gate B manifest 与 official score identity 不一致")
    if inputs.get("pre_gt_freeze_file_sha256") != file_sha256(freeze_path):
        raise V3EvalError("Gate B manifest 与 pre-GT freeze identity 不一致")
    score_inputs = score.get("inputs") or {}
    if score_inputs.get("candidate_file_sha256") != file_sha256(paired_path):
        raise V3EvalError("Gate B official score 与 candidate identity 不一致")
    frozen = manifest.get("frozen_identities") or {}
    if frozen.get("candidate_file_sha256") != file_sha256(paired_path):
        raise V3EvalError("Gate B final manifest 与 candidate identity 不一致")
    required = ("ground_truth_file_sha256", "ground_truth_attestation_file_sha256")
    if any(not SHA_RE.fullmatch(str(score_inputs.get(key, ""))) for key in required):
        raise V3EvalError("Gate B official score 缺少 Ground Truth identity")
    return {
        "gate_b_official_score_file_sha256": file_sha256(score_path),
        "gate_b_final_manifest_file_sha256": file_sha256(manifest_path),
        "ground_truth_file_sha256": score_inputs["ground_truth_file_sha256"],
        "ground_truth_attestation_file_sha256": score_inputs["ground_truth_attestation_file_sha256"],
    }


def candidate_stage(
    run_dir: Path,
    paired_path: Path,
    freeze_path: Path,
    retrieval_profile: str,
    score_path: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    _assert_mutable(run_dir)
    output = run_dir / "candidate.json"
    if output.exists():
        raise V3EvalError(f"拒绝覆盖已有 artifact: {output}")
    if retrieval_profile not in {"legacy", "financial_v2"}:
        raise V3EvalError("retrieval_profile 必须是 legacy 或 financial_v2")
    paired = load_json(paired_path)
    freeze = load_json(freeze_path)
    score_path = score_path or paired_path.with_name("score.json")
    manifest_path = manifest_path or paired_path.with_name("final_manifest.json")
    release_identities = _validate_gate_b_release(
        paired_path,
        freeze_path,
        score_path,
        manifest_path,
    )
    if paired.get("ground_truth_loaded") is not False or freeze.get("ground_truth_loaded") is not False:
        raise V3EvalError("candidate 只接受 pre-GT frozen Gate B artifacts")
    if paired.get("schema_version") != "router-v2-holdout-paired-candidates-v2":
        raise V3EvalError("paired_candidates schema 无效")
    if freeze.get("schema_version") != "router-v2-holdout-pre-gt-freeze-v1":
        raise V3EvalError("pre_gt_freeze schema 无效")
    if freeze.get("status") != "frozen":
        raise V3EvalError("Gate B pre_gt_freeze 未冻结")
    if (freeze.get("identities") or {}).get("candidate_file_sha256") != file_sha256(paired_path):
        raise V3EvalError("paired_candidates 与 pre_gt_freeze identity 不一致")
    source_cases = _cases(paired)
    _unique_ids(source_cases)
    cases = [
        {
            "case_id": str(case.get("case_id") or case.get("id")),
            "question": str(case.get("question", "")),
            "contexts": _candidate_contexts(case, retrieval_profile),
        }
        for case in source_cases
    ]
    if any(not case["question"] for case in cases):
        raise V3EvalError("candidate question 不能为空")
    source_ids = freeze.get("identities") or {}
    retrieval_config_sha = source_ids.get("retrieval_config_canonical_sha256")
    payload = {
        "schema_version": "rag-answer-v3-candidate-v2",
        "status": "frozen",
        "run_id": run_dir.name,
        "ground_truth_loaded": False,
        "retrieval_profile": retrieval_profile,
        "case_count": len(cases),
        "identities": {
            "corpus_file_sha256": source_ids.get("corpus_file_sha256"),
            "candidate_canonical_identity_sha256": canonical_sha256(cases),
            "ranking_sha256": paired.get("ranking_sha256"),
            "retrieval_config_sha256": retrieval_config_sha,
            "embedding_identity": source_ids.get("embedding_identity"),
            "gate_b_candidate_file_sha256": file_sha256(paired_path),
            "gate_b_pre_gt_freeze_file_sha256": file_sha256(freeze_path),
            **release_identities,
        },
        "cases": cases,
    }
    required = ("corpus_file_sha256", "ranking_sha256", "retrieval_config_sha256", "embedding_identity")
    if any(not payload["identities"].get(key) for key in required):
        raise V3EvalError("Gate B freeze 缺少 V3 required identity")
    provenance_sources = {
        "paired_candidates": paired_path,
        "pre_gt_freeze": freeze_path,
        "official_score": score_path,
        "final_manifest": manifest_path,
    }
    for name, source in provenance_sources.items():
        target = run_dir / GATE_B_ARTIFACT_LOCATORS[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise V3EvalError(f"拒绝覆盖 Gate B provenance artifact: {target}")
        shutil.copyfile(source, target)
    try:
        provenance = validate_gate_b_provenance(run_dir, payload)
    except ValueError as exc:
        raise V3EvalError(f"Gate B provenance contract failed: {exc}") from exc
    payload["gate_b_provenance"] = provenance
    write_new_json(output, payload)
    return payload


def _ledger_payload(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger = build_citation_ledger(contexts)
    return [
        {
            "citation_id": entry.citation_id,
            "content_sha256": entry.content_sha256,
            "source": entry.source,
            "page_number": entry.page_number,
            "content_type": entry.content_type,
        }
        for entry in ledger.values()
    ]


def _cost(usage: dict[str, Any]) -> dict[str, Any]:
    try:
        input_rate = float(settings.LLM_INPUT_COST_PER_1M)
        output_rate = float(settings.LLM_OUTPUT_COST_PER_1M)
        input_tokens = int(usage.get("input_tokens"))
        output_tokens = int(usage.get("output_tokens"))
    except (TypeError, ValueError):
        return {"status": "unavailable", "reason": "pricing_or_usage_unavailable"}
    value = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return {"status": "available", "currency": settings.COST_CURRENCY, "value": round(value, 10)}


def _verification_payload(result: Any) -> dict[str, Any] | None:
    return result.verification.model_dump() if result.verification is not None else None


async def _generate_one(case: dict[str, Any], profile: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await execute_answer_from_contexts(
            case["question"], case["contexts"], answer_profile=profile
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        refused = result.answer_status == "refused" or any(term in result.answer for term in REFUSAL_TERMS)
        structured = result.structured_answer.model_dump() if result.structured_answer else None
        return {
            "status": "refused" if refused else "accepted",
            "answer_text": result.answer,
            "structured_output": structured,
            "citation_ledger": _ledger_payload(result.contexts),
            "verification": _verification_payload(result),
            "refusal_code": result.refusal_code,
            "latency_ms": elapsed,
            "generation_ms": result.generation_ms,
            "verification_ms": result.verification_ms,
            "token_usage": result.usage,
            "estimated_cost": _cost(result.usage),
        }
    except Exception as exc:
        return {
            "status": "error",
            "answer_text": "",
            "structured_output": None,
            "citation_ledger": _ledger_payload(case["contexts"]),
            "verification": None,
            "refusal_code": None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "generation_ms": None,
            "verification_ms": None,
            "token_usage": {},
            "estimated_cost": {"status": "unavailable", "reason": "provider_error"},
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }


async def generate_stage(run_dir: Path) -> dict[str, Any]:
    _assert_mutable(run_dir)
    output = run_dir / "generation.json"
    if output.exists():
        raise V3EvalError(f"拒绝覆盖已有 artifact: {output}")
    if not settings.API_KEY:
        raise V3EvalError("真实 generation 要求 .env 中 API_KEY，禁止 mock mode")
    candidate_path = run_dir / "candidate.json"
    candidate = load_json(candidate_path)
    if candidate.get("status") != "frozen" or candidate.get("ground_truth_loaded") is not False:
        raise V3EvalError("candidate 未冻结")
    cases = []
    for case in _cases(candidate):
        outputs = {}
        for profile in PROFILES:
            outputs[profile] = await _generate_one(case, profile)
        cases.append({"case_id": case["case_id"], "outputs": outputs})
    prompt_identity = canonical_sha256({
        "legacy": __import__("app.services.rag_service", fromlist=["RAG_SYSTEM_PROMPT"]).RAG_SYSTEM_PROMPT,
        "verified_v3": __import__("app.services.rag_service", fromlist=["VERIFIED_V3_PROMPT"]).VERIFIED_V3_PROMPT,
    })
    payload = {
        "schema_version": "rag-answer-v3-generation-v1",
        "status": "completed",
        "run_id": run_dir.name,
        "ground_truth_loaded": False,
        "case_count": len(cases),
        "profiles": list(PROFILES),
        "pricing": {
            "input_cost_per_1m": settings.LLM_INPUT_COST_PER_1M,
            "output_cost_per_1m": settings.LLM_OUTPUT_COST_PER_1M,
            "currency": settings.COST_CURRENCY,
        },
        "identities": {
            "candidate_file_sha256": file_sha256(candidate_path),
            "prompt_config_sha256": prompt_identity,
            "model_identity": f"openai-compatible:{settings.BASE_URL}:{settings.MODEL}",
            "thresholds_file_sha256": file_sha256(THRESHOLDS),
        },
        "cases": cases,
    }
    write_new_json(output, payload)
    return payload


def _score_output(
    output: dict[str, Any],
    truth: dict[str, Any],
    profile: str,
    *,
    question: str | None = None,
    contexts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return contract_score_output(
        output,
        truth,
        profile,
        question=question,
        contexts=contexts,
    )


def _load_recomputed_contract(
    candidate: dict[str, Any],
    generation: dict[str, Any],
    bundle_root: Path,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    try:
        truths, attestation, bundle = load_official_bundle(bundle_root)
        thresholds = load_json(THRESHOLDS)
        scored_cases, metrics, gate = recompute_score(
            candidate_cases=_cases(candidate),
            generation_cases=_cases(generation),
            truths=truths,
            preregistration=load_json(PREREGISTRATION),
            thresholds=thresholds,
            pricing=generation.get("pricing") or {},
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise V3EvalError(f"official bundle/scoring contract failed: {exc}") from exc
    return scored_cases, metrics, gate, attestation, bundle


def score_stage(run_dir: Path) -> dict[str, Any]:
    _assert_mutable(run_dir)
    candidate_path = run_dir / "candidate.json"
    generation_path = run_dir / "generation.json"
    candidate = load_json(candidate_path)
    generation = load_json(generation_path)
    if (generation.get("identities") or {}).get("candidate_file_sha256") != file_sha256(
        candidate_path
    ):
        raise V3EvalError("generation 与 candidate identity 不一致")
    try:
        gate_b_provenance = validate_gate_b_provenance(run_dir, candidate)
    except ValueError as exc:
        raise V3EvalError(f"Gate B provenance contract failed: {exc}") from exc
    if candidate.get("gate_b_provenance") != gate_b_provenance:
        raise V3EvalError("candidate Gate B provenance descriptor 不一致")

    scored_cases, metrics, gate, attestation, bundle = _load_recomputed_contract(
        candidate, generation, OFFICIAL_BUNDLE_ROOT
    )
    candidate_identities = candidate.get("identities") or {}
    bundle_files = bundle["files"]
    if candidate_identities.get("ground_truth_file_sha256") != bundle_files[
        "ground_truth"
    ]["file_sha256"]:
        raise V3EvalError("V3 score Ground Truth 与 Gate B official identity 不一致")
    if candidate_identities.get(
        "ground_truth_attestation_file_sha256"
    ) != bundle_files["attestation"]["file_sha256"]:
        raise V3EvalError("V3 score attestation 与 Gate B official identity 不一致")

    implementation = implementation_descriptor(PROJECT_ROOT)
    payload = {
        "schema_version": "rag-answer-v3-score-v1",
        "status": "official",
        "provisional": False,
        "run_id": run_dir.name,
        "ground_truth_loaded": True,
        "case_count": len(scored_cases),
        "official_score_blockers": [],
        "attestation": attestation,
        "official_bundle": bundle,
        "gate_b_provenance": gate_b_provenance,
        "implementation": implementation,
        "identities": {
            "generation_file_sha256": file_sha256(generation_path),
            "ground_truth_file_sha256": bundle_files["ground_truth"]["file_sha256"],
            "ground_truth_attestation_file_sha256": bundle_files["attestation"][
                "file_sha256"
            ],
            "official_bundle_canonical_sha256": bundle["canonical_sha256"],
            "gate_b_provenance_canonical_sha256": gate_b_provenance[
                "canonical_sha256"
            ],
            "implementation_canonical_sha256": implementation["canonical_sha256"],
            "scorer_file_sha256": implementation["sources"][
                "evals/v3/scoring_contract.py"
            ],
        },
        "metrics": metrics,
        "gate_c": gate,
        "cases": scored_cases,
    }
    write_new_json(run_dir / "score.json", payload)
    return payload


def finalize_stage(run_dir: Path) -> dict[str, Any]:
    _assert_mutable(run_dir)
    candidate_path = run_dir / "candidate.json"
    generation_path = run_dir / "generation.json"
    score_path = run_dir / "score.json"
    candidate = load_json(candidate_path)
    generation = load_json(generation_path)
    score = load_json(score_path)
    if score.get("status") != "official" or score.get("provisional") is not False:
        raise V3EvalError("finalize 只接受完整 v2 contract 的 official score")
    if score.get("gate_c", {}).get("passed") is not True:
        raise V3EvalError("Gate C stored decision 未通过，不得 finalize")
    candidate_identities = candidate.get("identities") or {}
    generation_identities = generation.get("identities") or {}
    score_identities = score.get("identities") or {}
    if candidate.get("schema_version") != "rag-answer-v3-candidate-v2":
        raise V3EvalError("candidate schema 无效")
    if generation.get("schema_version") != "rag-answer-v3-generation-v1":
        raise V3EvalError("generation schema 无效")
    if score.get("schema_version") != "rag-answer-v3-score-v1":
        raise V3EvalError("score schema 无效")
    if any(
        payload.get("run_id") != run_dir.name for payload in (candidate, generation, score)
    ):
        raise V3EvalError("stage run_id 与 run directory 不一致")
    if candidate_identities.get("candidate_canonical_identity_sha256") != canonical_sha256(
        _cases(candidate)
    ):
        raise V3EvalError("candidate canonical identity 与 cases 不一致")
    if generation_identities.get("candidate_file_sha256") != file_sha256(candidate_path):
        raise V3EvalError("generation 与 candidate artifact identity 不一致")
    if score_identities.get("generation_file_sha256") != file_sha256(generation_path):
        raise V3EvalError("score 与 generation artifact identity 不一致")
    try:
        gate_b_provenance = validate_gate_b_provenance(run_dir, candidate)
    except ValueError as exc:
        raise V3EvalError(f"Gate B provenance contract failed: {exc}") from exc
    if candidate.get("gate_b_provenance") != gate_b_provenance:
        raise V3EvalError("candidate Gate B provenance descriptor 不一致")
    if score.get("gate_b_provenance") != gate_b_provenance:
        raise V3EvalError("score Gate B provenance descriptor 不一致")

    scored_cases, metrics, gate, attestation, bundle = _load_recomputed_contract(
        candidate, generation, OFFICIAL_BUNDLE_ROOT
    )
    try:
        assert_exact_score_contract(
            score.get("cases"),
            score.get("metrics"),
            score.get("gate_c"),
            scored_cases,
            metrics,
            gate,
        )
    except ValueError as exc:
        raise V3EvalError(str(exc)) from exc
    if gate.get("passed") is not True:
        raise V3EvalError("Gate C 重算未通过，不得 finalize")
    if score.get("attestation") != attestation or score.get("official_bundle") != bundle:
        raise V3EvalError("score official bundle/attestation 与固定 bundle 不一致")
    bundle_files = bundle["files"]
    for name, expected in {
        "ground_truth_file_sha256": bundle_files["ground_truth"]["file_sha256"],
        "ground_truth_attestation_file_sha256": bundle_files["attestation"][
            "file_sha256"
        ],
    }.items():
        if candidate_identities.get(name) != expected or score_identities.get(name) != expected:
            raise V3EvalError(f"{name} 与固定 official bundle 不一致")

    implementation = implementation_descriptor(PROJECT_ROOT)
    if score.get("implementation") != implementation:
        raise V3EvalError("score implementation source identity 与当前源码不一致")
    score_identities = score.get("identities") or {}
    if score_identities.get("official_bundle_canonical_sha256") != bundle[
        "canonical_sha256"
    ]:
        raise V3EvalError("score official bundle canonical identity 不一致")
    if score_identities.get("gate_b_provenance_canonical_sha256") != gate_b_provenance[
        "canonical_sha256"
    ]:
        raise V3EvalError("score Gate B provenance canonical identity 不一致")
    if score_identities.get("implementation_canonical_sha256") != implementation[
        "canonical_sha256"
    ]:
        raise V3EvalError("score implementation canonical identity 不一致")

    identities = {
        **{
            key: candidate["identities"][key]
            for key in (
                "corpus_file_sha256",
                "candidate_canonical_identity_sha256",
                "ranking_sha256",
                "retrieval_config_sha256",
                "embedding_identity",
                "gate_b_official_score_file_sha256",
                "gate_b_final_manifest_file_sha256",
                "ground_truth_file_sha256",
                "ground_truth_attestation_file_sha256",
            )
        },
        "candidate_file_sha256": file_sha256(candidate_path),
        "generation_file_sha256": file_sha256(generation_path),
        "score_file_sha256": file_sha256(score_path),
        "prompt_config_sha256": generation["identities"]["prompt_config_sha256"],
        "model_identity": generation["identities"]["model_identity"],
        "official_bundle_canonical_sha256": bundle["canonical_sha256"],
        "gate_b_provenance_canonical_sha256": gate_b_provenance["canonical_sha256"],
        "implementation_canonical_sha256": implementation["canonical_sha256"],
        "scorer_file_sha256": implementation["sources"][
            "evals/v3/scoring_contract.py"
        ],
    }
    payload = {
        "schema_version": "rag-answer-v3-run-manifest-v2",
        "status": "finalized",
        "immutable": True,
        "run_id": run_dir.name,
        "preregistration_sha256": file_sha256(PREREGISTRATION),
        "thresholds_sha256": file_sha256(THRESHOLDS),
        "case_count": candidate["case_count"],
        "identities": identities,
        "official_bundle": bundle,
        "gate_b_provenance": gate_b_provenance,
        "implementation": implementation,
        "stages": {
            "candidate": {
                "path": "candidate.json",
                "file_sha256": file_sha256(candidate_path),
            },
            "generation": {
                "path": "generation.json",
                "file_sha256": file_sha256(generation_path),
            },
            "score": {"path": "score.json", "file_sha256": file_sha256(score_path)},
        },
        "gate_c_passed": True,
        "claim_boundary": load_json(PREREGISTRATION)["claim_boundary"],
    }
    write_new_json(run_dir / "manifest.json", payload)
    return payload


def _attestation_payload(payload: Any) -> tuple[dict[str, Any] | None, bool, list[str]]:
    if not isinstance(payload, dict):
        return None, False, ["ground_truth_attestation_missing"]
    requirements = {"ranking_not_viewed": True, "human_review_status": "accepted", "reviewer_independence_declared": True}
    blockers = [key for key, expected in requirements.items() if payload.get(key) != expected]
    origin = " ".join(str(payload.get(key, "")).casefold() for key in ("attestation_type", "draft_origin", "reviewer_type", "created_by"))
    if any(marker in origin for marker in ("ai_agent", "ai-agent", "agent_draft")):
        blockers.append("ai_agent_draft_not_official")
    return payload, not blockers, blockers


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gate C V3 frozen-context real evaluation")
    parser.add_argument("--runs-root", type=Path, default=V3_DIR / "runs")
    parser.add_argument("--run-id", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    candidate = sub.add_parser("candidate")
    candidate.add_argument("--gate-b-run", type=Path, required=True)
    candidate.add_argument("--retrieval-profile", choices=("legacy", "financial_v2"), required=True)
    sub.add_parser("generate")
    score = sub.add_parser("score")
    finalize = sub.add_parser("finalize")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = _run_dir(args.runs_root, args.run_id)
    try:
        if args.command == "candidate":
            candidate_stage(
                run_dir,
                args.gate_b_run / "paired_candidates.json",
                args.gate_b_run / "pre_gt_freeze.json",
                args.retrieval_profile,
                args.gate_b_run / "score.json",
                args.gate_b_run / "final_manifest.json",
            )
        elif args.command == "generate":
            asyncio.run(generate_stage(run_dir))
        elif args.command == "score":
            score_stage(run_dir)
        else:
            finalize_stage(run_dir)
    except (OSError, json.JSONDecodeError, V3EvalError) as exc:
        print(f"[FAILED] {exc}")
        return 1
    print(json.dumps({"status": "passed", "run_id": args.run_id, "stage": args.command}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
