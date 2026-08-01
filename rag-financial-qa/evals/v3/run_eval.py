from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

V3_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = V3_DIR.parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)
os.environ.setdefault("DEBUG", "false")
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.services.answer_verification_service import build_citation_ledger
from app.services.rag_service import execute_answer_from_contexts
from app.utils.financial_normalization import (
    ecommerce_values_equal,
    normalize_ecommerce_value,
)
from app.utils.historical_financial_normalization import (
    financially_equal,
    normalize_financial_value,
)
from scripts.atomic_json import write_json_atomic

PREREGISTRATION = V3_DIR / "preregistration.json"
THRESHOLDS = V3_DIR / "thresholds.json"
PROFILES = ("legacy", "verified_v3")
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
    release_identities = _validate_gate_b_release(
        paired_path,
        freeze_path,
        score_path or paired_path.with_name("score.json"),
        manifest_path or paired_path.with_name("final_manifest.json"),
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


def _attestation(path: Path) -> tuple[dict[str, Any] | None, bool, list[str]]:
    if not path.is_file():
        return None, False, ["ground_truth_attestation_missing"]
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise V3EvalError("attestation 必须是对象")
    requirements = {
        "ranking_not_viewed": True,
        "human_review_status": "accepted",
        "reviewer_independence_declared": True,
    }
    blockers = [key for key, expected in requirements.items() if payload.get(key) != expected]
    origin = " ".join(str(payload.get(key, "")).casefold() for key in
                      ("attestation_type", "draft_origin", "reviewer_type", "created_by"))
    if any(marker in origin for marker in ("ai_agent", "ai-agent", "agent_draft")):
        blockers.append("ai_agent_draft_not_official")
    return payload, not blockers, blockers


def _truth_cases(path: Path, expected_ids: list[str]) -> list[dict[str, Any]]:
    cases = _cases(load_json(path))
    ids = _unique_ids(cases)
    if set(ids) != set(expected_ids) or len(ids) != len(expected_ids):
        raise V3EvalError("Ground Truth case ID/count 与 candidate 不一致")
    by_id = {case_id: case for case_id, case in zip(ids, cases)}
    return [by_id[case_id] for case_id in expected_ids]


def _norm(value: Any) -> str:
    return re.sub(r"[\s,，]", "", str(value or "")).casefold()


def _field_match(actual: Any, expected: Any) -> bool:
    return expected not in (None, "") and _norm(expected) == _norm(actual)


def _legacy_text_match(text: Any, expected: Any, *, numeric: bool = False) -> bool:
    if expected in (None, ""):
        return False
    actual = _norm(text)
    target = _norm(expected)
    if numeric:
        return re.search(rf"(?<![\d.]){re.escape(target)}(?![\d.])", actual) is not None
    if target == "元":
        return re.search(r"(?<!千|万|百|亿)元", actual) is not None
    return target in actual


def _financial_field_match(
    actual_value: Any,
    actual_unit: Any,
    expected_value: Any,
    expected_unit: Any,
) -> bool:
    if expected_value in (None, "") or expected_unit in (None, ""):
        return False
    actual = normalize_financial_value(str(actual_value or ""), str(actual_unit or "") or None)
    expected = normalize_financial_value(str(expected_value), str(expected_unit))
    return actual is not None and expected is not None and financially_equal(actual, expected)


def _citation_contract(output: dict[str, Any]) -> tuple[bool, bool]:
    ledger_ids = {
        str(item.get("citation_id", "")).upper()
        for item in output.get("citation_ledger") or []
        if item.get("citation_id")
    }
    answer_ids = {
        item.upper()
        for item in re.findall(r"\[(C\d+)\]", str(output.get("answer_text", "")), re.IGNORECASE)
    }
    fact_ids = {
        str(item).upper()
        for fact in ((output.get("structured_output") or {}).get("facts") or [])
        for item in (fact.get("citation_ids") or [])
    }
    unknown = bool((answer_ids | fact_ids) - ledger_ids)
    valid = bool(answer_ids) and answer_ids == fact_ids and not unknown
    return valid, unknown


def _ecommerce_field_match(fact: dict[str, Any], truth: dict[str, Any]) -> dict[str, bool]:
    fact_type = str(fact.get("fact_type") or "")
    expected_type = str(truth.get("fact_type") or "")
    if not expected_type:
        return {}
    unit = fact.get("currency") if fact_type == "price" else fact.get("unit")
    expected_unit = truth.get("expected_currency") or truth.get("expected_unit")
    actual = normalize_ecommerce_value(
        str(fact.get("value_text") or ""),
        fact.get("unit"),
        fact.get("currency"),
        fact_type=fact_type,
    )
    expected = normalize_ecommerce_value(
        str(truth.get("expected_value") or ""),
        truth.get("expected_unit"),
        truth.get("expected_currency"),
        fact_type=expected_type,
    )
    normalized_equal = bool(
        actual and expected and ecommerce_values_equal(actual, expected)
    )
    return {
        "numeric": normalized_equal,
        "unit": normalized_equal,
        "period": _field_match(fact.get("date"), truth.get("expected_date")),
        "company": _field_match(fact.get("product"), truth.get("expected_product")),
        "metric": _field_match(fact_type, expected_type),
        "scope": all(
            _field_match(fact.get(name), truth.get(f"expected_{name}"))
            for name in ("sku", "platform", "market")
            if truth.get(f"expected_{name}") not in (None, "")
        ),
    }


def _score_output(output: dict[str, Any], truth: dict[str, Any], profile: str) -> dict[str, Any]:
    should_refuse = bool(truth.get("should_refuse", False))
    accepted = output.get("status") == "accepted"
    refused = output.get("status") == "refused"
    structured = output.get("structured_output") or {}
    facts = structured.get("facts") or []
    fact = facts[0] if facts else {}
    text = output.get("answer_text", "")
    ecommerce_fields = _ecommerce_field_match(fact, truth) if profile == "verified_v3" else {}
    fields = ecommerce_fields or {
        "numeric": (
            _financial_field_match(
                fact.get("value_text"), fact.get("unit"),
                truth.get("expected_value"), truth.get("expected_unit"),
            )
            if profile == "verified_v3"
            else _legacy_text_match(text, truth.get("expected_value"), numeric=True)
        ),
        "unit": (_field_match(fact.get("unit"), truth.get("expected_unit")) if profile == "verified_v3" else _legacy_text_match(text, truth.get("expected_unit"))),
        "period": (_field_match(fact.get("year"), truth.get("expected_year")) if profile == "verified_v3" else _legacy_text_match(text, truth.get("expected_year"))),
        "company": (_field_match(fact.get("company"), truth.get("expected_company")) if profile == "verified_v3" else _legacy_text_match(text, truth.get("expected_company"))),
        "metric": (_field_match(fact.get("metric"), truth.get("metric")) if profile == "verified_v3" else _legacy_text_match(text, truth.get("metric"))),
        "scope": (_field_match(fact.get("scope"), truth.get("expected_scope")) if profile == "verified_v3" else _legacy_text_match(text, truth.get("expected_scope"))),
    }
    strict_correct = refused if should_refuse else accepted and all(fields.values())
    verification = output.get("verification") or {}
    errors = verification.get("errors") or []
    citation_contract_valid, citation_contract_unknown = _citation_contract(output)
    unknown = citation_contract_unknown or any("unknown_citation" in error for error in errors)
    unsupported_numeric = any(
        marker in error for error in errors
        for marker in ("uncited_numeric", "evidence_mismatch", "unsupported_citation")
    )
    schema_error = any("invalid_structured_output" in error for error in errors)
    citation_valid = (
        bool(verification.get("passed")) and citation_contract_valid
        if profile == "verified_v3" and accepted
        else None
    )
    return {
        "accepted": accepted,
        "refused": refused,
        "should_refuse": should_refuse,
        "strict_correct": strict_correct,
        **{f"{key}_correct": value for key, value in fields.items()},
        "citation_valid": citation_valid,
        "unknown_citation_accepted": accepted and unknown,
        "unsupported_numeric_accepted": accepted and unsupported_numeric,
        "schema_error": schema_error,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _aggregate(rows: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    accepted_rows = [row for row in rows if row["accepted"]]
    answerable = [row for row in rows if not row["should_refuse"]]
    refusal_truth = [row for row in rows if row["should_refuse"]]
    predicted_refusal = [row for row in rows if row["refused"]]
    valid_citations = [row for row in accepted_rows if row["citation_valid"] is not None]
    latencies = [int(output["latency_ms"]) for output in outputs if output.get("latency_ms") is not None]
    usages = [output.get("token_usage") or {} for output in outputs]
    costs = [output.get("estimated_cost") or {} for output in outputs]
    available_costs = [float(cost["value"]) for cost in costs if cost.get("status") == "available"]
    return {
        "retrieval_recall_at_5": None,
        "answer_coverage": _ratio(len(accepted_rows), len(answerable)),
        "accepted_answer_strict_precision": _ratio(sum(row["strict_correct"] for row in accepted_rows), len(accepted_rows)),
        "numeric_accuracy": _ratio(sum(row["numeric_correct"] for row in answerable), len(answerable)),
        "unit_accuracy": _ratio(sum(row["unit_correct"] for row in answerable), len(answerable)),
        "period_accuracy": _ratio(sum(row["period_correct"] for row in answerable), len(answerable)),
        "company_accuracy": _ratio(sum(row["company_correct"] for row in answerable), len(answerable)),
        "metric_accuracy": _ratio(sum(row["metric_correct"] for row in answerable), len(answerable)),
        "scope_accuracy": _ratio(sum(row["scope_correct"] for row in answerable), len(answerable)),
        "citation_validity": _ratio(sum(row["citation_valid"] is True for row in valid_citations), len(valid_citations)),
        "unknown_citation_acceptance_count": sum(row["unknown_citation_accepted"] for row in rows),
        "unsupported_numeric_acceptance_count": sum(row["unsupported_numeric_accepted"] for row in rows),
        "unsupported_answer_acceptance_rate": _ratio(sum(row["unsupported_numeric_accepted"] for row in rows), len(accepted_rows)),
        "refusal_precision": _ratio(sum(row["should_refuse"] for row in predicted_refusal), len(predicted_refusal)),
        "refusal_recall": _ratio(sum(row["refused"] for row in refusal_truth), len(refusal_truth)),
        "verification_reason_distribution": _reason_distribution(outputs),
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "input_tokens": sum(int(usage.get("input_tokens") or 0) for usage in usages),
        "output_tokens": sum(int(usage.get("output_tokens") or 0) for usage in usages),
        "estimated_cost": ({"status": "available", "currency": settings.COST_CURRENCY, "total": round(sum(available_costs), 10), "per_case": round(sum(available_costs) / len(outputs), 10)} if len(available_costs) == len(outputs) else {"status": "unavailable"}),
        "provider_error_rate": _ratio(sum(output.get("status") == "error" for output in outputs), len(outputs)),
        "schema_error_rate": _ratio(sum(row["schema_error"] for row in rows), len(rows)),
    }


def _reason_distribution(outputs: list[dict[str, Any]]) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for output in outputs:
        verification = output.get("verification") or {}
        reasons = verification.get("errors") or ([verification.get("status")] if verification else [output.get("status")])
        for reason in reasons:
            if reason:
                distribution[str(reason)] = distribution.get(str(reason), 0) + 1
    return distribution


def _retrieval_recall(candidate_cases: list[dict[str, Any]], truths: list[dict[str, Any]]) -> float:
    hits = 0
    eligible = 0
    for case, truth in zip(candidate_cases, truths):
        if truth.get("should_refuse"):
            continue
        expected_source = truth.get("expected_source") or truth.get("pdf")
        expected_page = truth.get("expected_page")
        if expected_source in (None, "") and expected_page in (None, ""):
            continue
        eligible += 1
        for context in case["contexts"][:5]:
            source_ok = (
                expected_source in (None, "")
                or Path(str(context.get("source", ""))).name.casefold()
                == Path(str(expected_source)).name.casefold()
            )
            page_ok = expected_page in (None, "") or str(context.get("page_number")) == str(expected_page)
            if source_ok and page_ok:
                hits += 1
                break
    return _ratio(hits, eligible)


def _gate(metrics: dict[str, dict[str, Any]]) -> dict[str, Any]:
    prereg = load_json(PREREGISTRATION)
    thresholds = load_json(THRESHOLDS)["thresholds"]
    v3 = metrics["verified_v3"]
    legacy = metrics["legacy"]
    cost = v3["estimated_cost"]
    checks = {
        "accepted_answer_strict_precision_min": v3["accepted_answer_strict_precision"] >= float(prereg["independent_quality_gates"]["accepted_answer_strict_precision_min"]),
        "citation_validity": v3["citation_validity"] >= float(prereg["engineering_gates"]["accepted_answer_citation_validity_min"]),
        "unknown_citation": v3["unknown_citation_acceptance_count"] <= int(prereg["engineering_gates"]["unknown_citation_acceptance_max"]),
        "unsupported_numeric": v3["unsupported_numeric_acceptance_count"] <= int(prereg["engineering_gates"]["unsupported_numeric_acceptance_max"]),
        "schema_error": v3["schema_error_rate"] <= float(prereg["engineering_gates"]["schema_error_rate_max"]),
        "not_underperform_same_candidate_legacy": v3["accepted_answer_strict_precision"] >= legacy["accepted_answer_strict_precision"],
        "answer_coverage_min": v3["answer_coverage"] >= float(thresholds["answer_coverage_min"]),
        "latency_p95_ms_max": v3["latency_p95_ms"] is not None and v3["latency_p95_ms"] <= int(thresholds["latency_p95_ms_max"]),
        "estimated_cost_per_case_max": cost.get("status") == "available" and cost.get("per_case", float("inf")) <= float(thresholds["estimated_cost_per_case_max"]),
    }
    return {"passed": all(checks.values()), "checks": checks}


def score_stage(run_dir: Path, ground_truth_path: Path, attestation_path: Path) -> dict[str, Any]:
    _assert_mutable(run_dir)
    candidate_path = run_dir / "candidate.json"
    generation_path = run_dir / "generation.json"
    candidate = load_json(candidate_path)
    generation = load_json(generation_path)
    candidate_identities = candidate.get("identities") or {}
    if candidate_identities.get("ground_truth_file_sha256") != file_sha256(ground_truth_path):
        raise V3EvalError("V3 score Ground Truth 与 Gate B official identity 不一致")
    if not attestation_path.is_file():
        raise V3EvalError("V3 score attestation 不存在或与 Gate B official identity 不一致")
    if candidate_identities.get("ground_truth_attestation_file_sha256") != file_sha256(attestation_path):
        raise V3EvalError("V3 score attestation 与 Gate B official identity 不一致")
    if (generation.get("identities") or {}).get("candidate_file_sha256") != file_sha256(candidate_path):
        raise V3EvalError("generation 与 candidate identity 不一致")
    candidate_cases = _cases(candidate)
    generation_cases = _cases(generation)
    ids = _unique_ids(candidate_cases)
    if _unique_ids(generation_cases) != ids:
        raise V3EvalError("generation case ID/order 与 candidate 不一致")
    truths = _truth_cases(ground_truth_path, ids)
    attestation, official, blockers = _attestation(attestation_path)
    scored_cases = []
    profile_rows = {profile: [] for profile in PROFILES}
    profile_outputs = {profile: [] for profile in PROFILES}
    for generated, truth in zip(generation_cases, truths):
        scores = {}
        for profile in PROFILES:
            output = generated["outputs"][profile]
            row = _score_output(output, truth, profile)
            scores[profile] = row
            profile_rows[profile].append(row)
            profile_outputs[profile].append(output)
        scored_cases.append({"case_id": generated["case_id"], "scores": scores})
    recall = _retrieval_recall(candidate_cases, truths)
    metrics = {profile: _aggregate(profile_rows[profile], profile_outputs[profile]) for profile in PROFILES}
    for profile in PROFILES:
        metrics[profile]["retrieval_recall_at_5"] = recall
    gate = _gate(metrics)
    payload = {
        "schema_version": "rag-answer-v3-score-v1",
        "status": "official" if official else "provisional",
        "provisional": not official,
        "run_id": run_dir.name,
        "ground_truth_loaded": True,
        "case_count": len(scored_cases),
        "official_score_blockers": blockers,
        "attestation": attestation,
        "identities": {
            "generation_file_sha256": file_sha256(generation_path),
            "ground_truth_file_sha256": file_sha256(ground_truth_path),
            "ground_truth_attestation_file_sha256": file_sha256(attestation_path) if attestation else None,
            "scorer_file_sha256": file_sha256(Path(__file__).resolve()),
        },
        "metrics": metrics,
        "gate_c": gate,
        "cases": scored_cases,
    }
    write_new_json(run_dir / ("score.json" if official else "score_provisional.json"), payload)
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
        raise V3EvalError("finalize 只接受独立人工 attestation 的 official score")
    if not score.get("gate_c", {}).get("passed"):
        raise V3EvalError("Gate C 未通过，不得 finalize")
    _, official, blockers = _attestation_payload(score.get("attestation"))
    if not official:
        raise V3EvalError(f"official attestation 边界不满足: {blockers}")
    identities = {
        **{key: candidate["identities"][key] for key in (
            "corpus_file_sha256", "candidate_canonical_identity_sha256", "ranking_sha256",
            "retrieval_config_sha256", "embedding_identity", "gate_b_official_score_file_sha256",
            "gate_b_final_manifest_file_sha256", "ground_truth_file_sha256",
            "ground_truth_attestation_file_sha256",
        )},
        "candidate_file_sha256": file_sha256(candidate_path),
        "generation_file_sha256": file_sha256(generation_path),
        "score_file_sha256": file_sha256(score_path),
        "prompt_config_sha256": generation["identities"]["prompt_config_sha256"],
        "model_identity": generation["identities"]["model_identity"],
        "scorer_file_sha256": score["identities"]["scorer_file_sha256"],
    }
    payload = {
        "schema_version": "rag-answer-v3-run-manifest-v1",
        "status": "finalized",
        "immutable": True,
        "run_id": run_dir.name,
        "preregistration_sha256": file_sha256(PREREGISTRATION),
        "thresholds_sha256": file_sha256(THRESHOLDS),
        "case_count": candidate["case_count"],
        "identities": identities,
        "stages": {
            "candidate": {"path": "candidate.json", "file_sha256": file_sha256(candidate_path)},
            "generation": {"path": "generation.json", "file_sha256": file_sha256(generation_path)},
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
    score.add_argument("--ground-truth", type=Path, required=True)
    score.add_argument("--attestation", type=Path, required=True)
    sub.add_parser("finalize")
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
            score_stage(run_dir, args.ground_truth.resolve(), args.attestation.resolve())
        else:
            finalize_stage(run_dir)
    except (OSError, json.JSONDecodeError, V3EvalError) as exc:
        print(f"[FAILED] {exc}")
        return 1
    print(json.dumps({"status": "passed", "run_id": args.run_id, "stage": args.command}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
