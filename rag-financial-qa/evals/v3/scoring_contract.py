from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from app.services.answer_verification_service import (
    build_citation_ledger,
    verify_structured_answer,
)
from app.utils.financial_normalization import (
    ecommerce_values_equal,
    normalize_ecommerce_value,
)
from app.utils.historical_financial_normalization import (
    financially_equal,
    normalize_financial_value,
)
from evals.common.ground_truth_contract import validate_official_bundle

PROFILES = ("legacy", "verified_v3")
BUNDLE_ROOT_LOCATOR = "evals/router_v2_holdout"
OFFICIAL_BUNDLE_LOCATORS = {
    "query_only": "query_only.jsonl",
    "source_manifest": "source_manifest.json",
    "preregistration": "preregistration.json",
    "ground_truth": "private/ground_truth.json",
    "attestation": "private/ground_truth_attestation.json",
}
IMPLEMENTATION_SOURCE_LOCATORS = (
    "evals/v3/run_eval.py",
    "evals/v3/scoring_contract.py",
    "evals/v3/validate_run.py",
    "evals/common/ground_truth_contract.py",
    "app/services/answer_verification_service.py",
    "app/schemas/schemas.py",
    "app/utils/financial_normalization.py",
    "app/utils/historical_financial_normalization.py",
    "app/utils/retrieval.py",
)
GATE_B_ARTIFACT_LOCATORS = {
    "paired_candidates": "gate_b/paired_candidates.json",
    "pre_gt_freeze": "gate_b/pre_gt_freeze.json",
    "official_score": "gate_b/score.json",
    "final_manifest": "gate_b/final_manifest.json",
}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def assert_exact_score_contract(
    stored_cases: Any,
    stored_metrics: Any,
    stored_gate: Any,
    recomputed_cases: list[dict[str, Any]],
    recomputed_metrics: dict[str, dict[str, Any]],
    recomputed_gate: dict[str, Any],
) -> None:
    comparisons = {
        "score cases": (stored_cases, recomputed_cases),
        "score metrics": (stored_metrics, recomputed_metrics),
        "Gate C decision": (stored_gate, recomputed_gate),
    }
    for label, (stored, recomputed) in comparisons.items():
        if canonical_json(stored) != canonical_json(recomputed):
            raise ValueError(f"{label} does not exactly match deterministic recomputation")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL row must be an object: {path}")
            rows.append(payload)
    if not rows:
        raise ValueError(f"JSONL must contain at least one row: {path}")
    return rows


def gate_b_artifact_paths(run_dir: Path) -> dict[str, Path]:
    root = run_dir.resolve()
    paths = {}
    for name, locator in GATE_B_ARTIFACT_LOCATORS.items():
        path = (root / locator).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Gate B artifact locator escapes run directory: {name}") from exc
        paths[name] = path
    return paths


def validate_gate_b_provenance(
    run_dir: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    paths = gate_b_artifact_paths(run_dir)
    for name, path in paths.items():
        if not path.is_file():
            raise ValueError(f"Gate B provenance artifact is missing: {name}")
    paired = _load_json(paths["paired_candidates"])
    freeze = _load_json(paths["pre_gt_freeze"])
    score = _load_json(paths["official_score"])
    final_manifest = _load_json(paths["final_manifest"])
    if paired.get("schema_version") != "router-v2-holdout-paired-candidates-v2":
        raise ValueError("Gate B paired candidate schema is invalid")
    if paired.get("ground_truth_loaded") is not False:
        raise ValueError("Gate B paired candidate must remain pre-GT")
    if (
        freeze.get("schema_version") != "router-v2-holdout-pre-gt-freeze-v1"
        or freeze.get("status") != "frozen"
        or freeze.get("ground_truth_loaded") is not False
    ):
        raise ValueError("Gate B pre-GT freeze is invalid")
    if (freeze.get("identities") or {}).get("candidate_file_sha256") != file_sha256(
        paths["paired_candidates"]
    ):
        raise ValueError("Gate B freeze does not bind paired candidate")
    if (
        score.get("schema_version") != "router-v2-holdout-score-v1"
        or score.get("status") != "official"
        or score.get("provisional") is not False
        or score.get("gate_b", {}).get("passed") is not True
    ):
        raise ValueError("Gate B official score is invalid")
    if (score.get("inputs") or {}).get("candidate_file_sha256") != file_sha256(
        paths["paired_candidates"]
    ):
        raise ValueError("Gate B score does not bind paired candidate")
    if (
        final_manifest.get("schema_version")
        != "router-v2-holdout-final-manifest-v1"
        or final_manifest.get("status") != "finalized"
        or final_manifest.get("immutable") is not True
        or final_manifest.get("gate_b_passed") is not True
    ):
        raise ValueError("Gate B final manifest is invalid")
    manifest_inputs = final_manifest.get("inputs") or {}
    if manifest_inputs.get("official_score_file_sha256") != file_sha256(
        paths["official_score"]
    ):
        raise ValueError("Gate B final manifest does not bind official score")
    if manifest_inputs.get("pre_gt_freeze_file_sha256") != file_sha256(
        paths["pre_gt_freeze"]
    ):
        raise ValueError("Gate B final manifest does not bind pre-GT freeze")
    if (final_manifest.get("frozen_identities") or {}).get(
        "candidate_file_sha256"
    ) != file_sha256(paths["paired_candidates"]):
        raise ValueError("Gate B final manifest does not bind paired candidate")

    profile = candidate.get("retrieval_profile")
    if profile not in {"legacy", "financial_v2"}:
        raise ValueError("V3 candidate retrieval profile is invalid")
    paired_cases = paired.get("cases")
    if not isinstance(paired_cases, list) or not paired_cases:
        raise ValueError("Gate B paired candidate cases are invalid")
    rebuilt_cases = []
    for case in paired_cases:
        selected = (case.get("profiles") or {}).get(profile) or {}
        contexts = selected.get("top_k")
        if not isinstance(contexts, list):
            raise ValueError(f"Gate B candidate profile is missing: {profile}")
        rebuilt_cases.append(
            {
                "case_id": str(case.get("case_id") or case.get("id") or ""),
                "question": str(case.get("question", "")),
                "contexts": [dict(item) for item in contexts],
            }
        )
    if canonical_json(candidate.get("cases")) != canonical_json(rebuilt_cases):
        raise ValueError("V3 candidate cases do not exactly match frozen Gate B artifacts")

    candidate_identities = candidate.get("identities") or {}
    freeze_identities = freeze.get("identities") or {}
    source_identity_links = {
        "ranking_sha256": paired.get("ranking_sha256"),
        "corpus_file_sha256": freeze_identities.get("corpus_file_sha256"),
        "retrieval_config_sha256": freeze_identities.get(
            "retrieval_config_canonical_sha256"
        ),
        "embedding_identity": freeze_identities.get("embedding_identity"),
    }
    for name, expected_value in source_identity_links.items():
        if not expected_value or candidate_identities.get(name) != expected_value:
            raise ValueError(f"V3 candidate Gate B source identity mismatch: {name}")
    score_inputs = score.get("inputs") or {}
    for name in (
        "ground_truth_file_sha256",
        "ground_truth_attestation_file_sha256",
    ):
        if not score_inputs.get(name) or candidate_identities.get(name) != score_inputs.get(name):
            raise ValueError(f"V3 candidate Gate B official identity mismatch: {name}")

    expected = {
        "gate_b_candidate_file_sha256": file_sha256(paths["paired_candidates"]),
        "gate_b_pre_gt_freeze_file_sha256": file_sha256(paths["pre_gt_freeze"]),
        "gate_b_official_score_file_sha256": file_sha256(paths["official_score"]),
        "gate_b_final_manifest_file_sha256": file_sha256(paths["final_manifest"]),
    }
    for name, value in expected.items():
        if candidate_identities.get(name) != value:
            raise ValueError(f"V3 candidate Gate B provenance identity mismatch: {name}")
    descriptor = {
        "schema_version": "rag-answer-v3-gate-b-provenance-v1",
        "artifacts": {
            name: {"path": GATE_B_ARTIFACT_LOCATORS[name], "file_sha256": file_sha256(path)}
            for name, path in paths.items()
        },
    }
    descriptor["canonical_sha256"] = canonical_sha256(descriptor["artifacts"])
    return descriptor


def official_bundle_paths(bundle_root: Path) -> dict[str, Path]:
    root = bundle_root.resolve()
    paths: dict[str, Path] = {}
    for name, locator in OFFICIAL_BUNDLE_LOCATORS.items():
        path = (root / locator).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"official bundle locator escapes bundle root: {name}") from exc
        paths[name] = path
    return paths


def load_official_bundle(
    bundle_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    paths = official_bundle_paths(bundle_root)
    queries = _load_jsonl(paths["query_only"])
    source_manifest = _load_json(paths["source_manifest"])
    if not isinstance(source_manifest, list) or not source_manifest:
        raise ValueError("official source manifest must be a non-empty array")
    truths, attestation = validate_official_bundle(
        ground_truth_path=paths["ground_truth"],
        attestation_path=paths["attestation"],
        query_only_path=paths["query_only"],
        source_manifest_path=paths["source_manifest"],
        preregistration_path=paths["preregistration"],
        queries=queries,
        source_manifest=source_manifest,
    )
    descriptor = {
        "schema_version": "router-v2-official-bundle-locator-v1",
        "root_locator": BUNDLE_ROOT_LOCATOR,
        "files": {
            name: {
                "path": OFFICIAL_BUNDLE_LOCATORS[name],
                "file_sha256": file_sha256(path),
            }
            for name, path in paths.items()
        },
    }
    descriptor["canonical_sha256"] = canonical_sha256(
        {
            "schema_version": descriptor["schema_version"],
            "root_locator": descriptor["root_locator"],
            "files": descriptor["files"],
        }
    )
    return truths, attestation, descriptor


def implementation_descriptor(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    sources = {}
    for locator in IMPLEMENTATION_SOURCE_LOCATORS:
        path = (root / locator).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"implementation locator escapes project root: {locator}") from exc
        if not path.is_file():
            raise ValueError(f"implementation source is missing: {locator}")
        sources[locator] = file_sha256(path)
    return {
        "schema_version": "rag-answer-v3-implementation-identity-v1",
        "sources": sources,
        "canonical_sha256": canonical_sha256(sources),
    }


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


def _ecommerce_field_match(
    fact: dict[str, Any], truth: dict[str, Any]
) -> dict[str, bool]:
    fact_type = str(fact.get("fact_type") or "")
    expected_type = str(truth.get("fact_type") or "")
    if not expected_type:
        return {}
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
        "company": _field_match(
            fact.get("product"), truth.get("expected_product")
        ),
        "metric": _field_match(fact_type, expected_type),
        "scope": all(
            _field_match(fact.get(name), truth.get(f"expected_{name}"))
            for name in ("sku", "platform", "market")
            if truth.get(f"expected_{name}") not in (None, "")
        ),
    }


def _financial_values_from_evidence(content: str) -> list[Any]:
    values = []
    pattern = re.compile(
        r"(?<![\d.])(?:\(\s*)?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
        r"(?:\s*\))?\s*(?:百分点|百万元|千元|万元|亿元|bp|bps|%|元)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(str(content)):
        parsed = normalize_financial_value(match.group(0))
        if parsed is not None:
            values.append(parsed)
    return values


def _historical_financial_verification(
    output: dict[str, Any],
    fact: dict[str, Any],
    truth: dict[str, Any],
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    ledger = build_citation_ledger(contexts)
    answer_ids = {
        item.upper()
        for item in re.findall(
            r"\[(C\d+)\]", str(output.get("answer_text", "")), re.IGNORECASE
        )
    }
    fact_ids = {
        str(item).upper() for item in (fact.get("citation_ids") or [])
    }
    errors: list[str] = []
    unknown = sorted((answer_ids | fact_ids) - set(ledger))
    if unknown:
        errors.append(f"unknown_citation:{','.join(unknown)}")
    if not answer_ids:
        errors.append("answer_missing_citation")
    if answer_ids != fact_ids:
        errors.append("answer_fact_citation_mismatch")

    expected = normalize_financial_value(
        str(truth.get("expected_value") or ""),
        str(truth.get("expected_unit") or "") or None,
    )
    identity_terms = [
        str(truth.get(name) or "")
        for name in ("expected_company", "expected_year", "metric", "expected_scope")
        if truth.get(name) not in (None, "")
    ]
    supported = False
    for citation_id in sorted(answer_ids & fact_ids & set(ledger)):
        entry = ledger[citation_id]
        content = str(entry.content)
        values = _financial_values_from_evidence(content)
        value_supported = bool(
            expected
            and any(financially_equal(actual, expected) for actual in values)
        )
        identity_supported = all(_norm(term) in _norm(content) for term in identity_terms)
        if value_supported and identity_supported:
            supported = True
            break
    if not supported:
        errors.append("evidence_mismatch")
    return {
        "passed": not errors,
        "status": "passed" if not errors else "failed",
        "errors": errors,
    }


def score_output(
    output: dict[str, Any],
    truth: dict[str, Any],
    profile: str,
    *,
    question: str | None = None,
    contexts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    should_refuse = truth["should_refuse"]
    accepted = output.get("status") == "accepted"
    refused = output.get("status") == "refused"
    structured = output.get("structured_output") or {}
    facts = structured.get("facts") or []
    fact = facts[0] if facts else {}
    text = output.get("answer_text", "")
    ecommerce_fields = (
        _ecommerce_field_match(fact, truth) if profile == "verified_v3" else {}
    )
    fields = ecommerce_fields or {
        "numeric": (
            _financial_field_match(
                fact.get("value_text"),
                fact.get("unit"),
                truth.get("expected_value"),
                truth.get("expected_unit"),
            )
            if profile == "verified_v3"
            else _legacy_text_match(text, truth.get("expected_value"), numeric=True)
        ),
        "unit": (
            _field_match(fact.get("unit"), truth.get("expected_unit"))
            if profile == "verified_v3"
            else _legacy_text_match(text, truth.get("expected_unit"))
        ),
        "period": (
            _field_match(fact.get("year"), truth.get("expected_year"))
            if profile == "verified_v3"
            else _legacy_text_match(text, truth.get("expected_year"))
        ),
        "company": (
            _field_match(fact.get("company"), truth.get("expected_company"))
            if profile == "verified_v3"
            else _legacy_text_match(text, truth.get("expected_company"))
        ),
        "metric": (
            _field_match(fact.get("metric"), truth.get("metric"))
            if profile == "verified_v3"
            else _legacy_text_match(text, truth.get("metric"))
        ),
        "scope": (
            _field_match(fact.get("scope"), truth.get("expected_scope"))
            if profile == "verified_v3"
            else _legacy_text_match(text, truth.get("expected_scope"))
        ),
    }
    strict_correct = refused if should_refuse else accepted and all(fields.values())
    verification = output.get("verification") or {}
    if profile == "verified_v3":
        if question is None or contexts is None:
            raise ValueError("verified_v3 scoring requires question and frozen contexts")
        if truth.get("fact_type"):
            structured_for_verification = dict(structured)
            structured_for_verification["answer_text"] = text
            verification = verify_structured_answer(
                question,
                structured_for_verification,
                build_citation_ledger(contexts),
            ).model_dump()
        else:
            verification = _historical_financial_verification(
                output,
                fact,
                truth,
                contexts,
            )
    errors = verification.get("errors") or []
    unknown = any("unknown_citation" in error for error in errors)
    unsupported_numeric = any(
        marker in error
        for error in errors
        for marker in ("uncited_numeric", "evidence_mismatch", "unsupported_citation")
    )
    schema_error = any("invalid_structured_output" in error for error in errors)
    citation_valid = (
        bool(verification.get("passed"))
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
        "verification_status": verification.get("status"),
        "verification_errors": [str(error) for error in errors],
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _reason_distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for row in rows:
        reasons = row.get("verification_errors") or [row.get("verification_status")]
        for reason in reasons:
            if reason:
                distribution[str(reason)] = distribution.get(str(reason), 0) + 1
    return distribution


def _recomputed_cost(
    outputs: list[dict[str, Any]],
    pricing: dict[str, Any],
) -> dict[str, Any]:
    try:
        input_rate = float(pricing["input_cost_per_1m"])
        output_rate = float(pricing["output_cost_per_1m"])
        currency = str(pricing["currency"])
        if input_rate < 0 or output_rate < 0 or not currency:
            raise ValueError
        usages = [output.get("token_usage") or {} for output in outputs]
        input_tokens = [int(usage["input_tokens"]) for usage in usages]
        output_tokens = [int(usage["output_tokens"]) for usage in usages]
        if any(value < 0 for value in [*input_tokens, *output_tokens]):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return {"status": "unavailable"}
    total = (
        sum(input_tokens) * input_rate + sum(output_tokens) * output_rate
    ) / 1_000_000
    return {
        "status": "available",
        "currency": currency,
        "total": round(total, 10),
        "per_case": round(total / len(outputs), 10),
    }


def aggregate(
    rows: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    *,
    pricing: dict[str, Any],
) -> dict[str, Any]:
    accepted_rows = [row for row in rows if row["accepted"]]
    answerable = [row for row in rows if not row["should_refuse"]]
    accepted_answerable = [row for row in answerable if row["accepted"]]
    refusal_truth = [row for row in rows if row["should_refuse"]]
    predicted_refusal = [row for row in rows if row["refused"]]
    valid_citations = [row for row in accepted_rows if row["citation_valid"] is not None]
    latencies = [
        int(output["latency_ms"])
        for output in outputs
        if output.get("latency_ms") is not None
    ]
    usages = [output.get("token_usage") or {} for output in outputs]
    return {
        "retrieval_recall_at_5": None,
        "answer_coverage": _ratio(len(accepted_answerable), len(answerable)),
        "accepted_answer_strict_precision": _ratio(
            sum(row["strict_correct"] for row in accepted_rows), len(accepted_rows)
        ),
        "numeric_accuracy": _ratio(
            sum(row["numeric_correct"] for row in answerable), len(answerable)
        ),
        "unit_accuracy": _ratio(
            sum(row["unit_correct"] for row in answerable), len(answerable)
        ),
        "period_accuracy": _ratio(
            sum(row["period_correct"] for row in answerable), len(answerable)
        ),
        "company_accuracy": _ratio(
            sum(row["company_correct"] for row in answerable), len(answerable)
        ),
        "metric_accuracy": _ratio(
            sum(row["metric_correct"] for row in answerable), len(answerable)
        ),
        "scope_accuracy": _ratio(
            sum(row["scope_correct"] for row in answerable), len(answerable)
        ),
        "citation_validity": _ratio(
            sum(row["citation_valid"] is True for row in valid_citations),
            len(valid_citations),
        ),
        "unknown_citation_acceptance_count": sum(
            row["unknown_citation_accepted"] for row in rows
        ),
        "unsupported_numeric_acceptance_count": sum(
            row["unsupported_numeric_accepted"] for row in rows
        ),
        "unsupported_answer_acceptance_rate": _ratio(
            sum(row["unsupported_numeric_accepted"] for row in rows), len(accepted_rows)
        ),
        "refusal_precision": _ratio(
            sum(row["should_refuse"] for row in predicted_refusal),
            len(predicted_refusal),
        ),
        "refusal_recall": _ratio(
            sum(row["refused"] for row in refusal_truth), len(refusal_truth)
        ),
        "verification_reason_distribution": _reason_distribution(rows),
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "input_tokens": sum(int(usage.get("input_tokens") or 0) for usage in usages),
        "output_tokens": sum(int(usage.get("output_tokens") or 0) for usage in usages),
        "estimated_cost": _recomputed_cost(outputs, pricing),
        "provider_error_rate": _ratio(
            sum(output.get("status") == "error" for output in outputs), len(outputs)
        ),
        "schema_error_rate": _ratio(
            sum(row["schema_error"] for row in rows), len(rows)
        ),
    }


def retrieval_recall(
    candidate_cases: list[dict[str, Any]], truths: list[dict[str, Any]]
) -> float:
    hits = 0
    eligible = 0
    for case, truth in zip(candidate_cases, truths):
        if truth["should_refuse"]:
            continue
        expected_source = truth.get("expected_source") or truth.get("pdf")
        expected_page = truth.get("expected_page")
        if expected_source in (None, "") and expected_page in (None, ""):
            continue
        eligible += 1
        for context in case["contexts"][:5]:
            source_ok = expected_source in (None, "") or _norm(expected_source) in _norm(
                context.get("source")
            )
            page_ok = expected_page in (None, "") or str(context.get("page_number")) == str(
                expected_page
            )
            if source_ok and page_ok:
                hits += 1
                break
    return _ratio(hits, eligible)


def gate(
    metrics: dict[str, dict[str, Any]],
    preregistration: dict[str, Any],
    thresholds_payload: dict[str, Any],
) -> dict[str, Any]:
    thresholds = thresholds_payload["thresholds"]
    v3 = metrics["verified_v3"]
    legacy = metrics["legacy"]
    cost = v3["estimated_cost"]
    checks = {
        "accepted_answer_strict_precision_min": v3["accepted_answer_strict_precision"]
        >= float(
            preregistration["independent_quality_gates"][
                "accepted_answer_strict_precision_min"
            ]
        ),
        "citation_validity": v3["citation_validity"]
        >= float(
            preregistration["engineering_gates"][
                "accepted_answer_citation_validity_min"
            ]
        ),
        "unknown_citation": v3["unknown_citation_acceptance_count"]
        <= int(preregistration["engineering_gates"]["unknown_citation_acceptance_max"]),
        "unsupported_numeric": v3["unsupported_numeric_acceptance_count"]
        <= int(
            preregistration["engineering_gates"]["unsupported_numeric_acceptance_max"]
        ),
        "schema_error": v3["schema_error_rate"]
        <= float(preregistration["engineering_gates"]["schema_error_rate_max"]),
        "not_underperform_same_candidate_legacy": v3[
            "accepted_answer_strict_precision"
        ]
        >= legacy["accepted_answer_strict_precision"],
        "answer_coverage_min": v3["answer_coverage"]
        >= float(thresholds["answer_coverage_min"]),
        "latency_p95_ms_max": v3["latency_p95_ms"] is not None
        and v3["latency_p95_ms"] <= int(thresholds["latency_p95_ms_max"]),
        "estimated_cost_per_case_max": cost.get("status") == "available"
        and cost.get("per_case", float("inf"))
        <= float(thresholds["estimated_cost_per_case_max"]),
    }
    return {"passed": all(checks.values()), "checks": checks}


def recompute_score(
    *,
    candidate_cases: list[dict[str, Any]],
    generation_cases: list[dict[str, Any]],
    truths: list[dict[str, Any]],
    preregistration: dict[str, Any],
    thresholds: dict[str, Any],
    pricing: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    candidate_ids = [str(case.get("case_id", "")) for case in candidate_cases]
    generation_ids = [str(case.get("case_id", "")) for case in generation_cases]
    truth_ids = [str(case.get("case_id", "")) for case in truths]
    if not candidate_ids or candidate_ids != generation_ids or candidate_ids != truth_ids:
        raise ValueError("candidate, generation, and normalized truth case order must match")
    if len(candidate_ids) != len(set(candidate_ids)) or any(not value for value in candidate_ids):
        raise ValueError("case IDs must be non-empty and unique")

    scored_cases = []
    profile_rows = {profile: [] for profile in PROFILES}
    profile_outputs = {profile: [] for profile in PROFILES}
    for candidate, generated, truth in zip(candidate_cases, generation_cases, truths):
        outputs = generated.get("outputs") or {}
        scores = {}
        for profile in PROFILES:
            output = outputs.get(profile)
            if not isinstance(output, dict):
                raise ValueError(f"generation output is missing for profile: {profile}")
            row = score_output(
                output,
                truth,
                profile,
                question=str(candidate.get("question", "")),
                contexts=candidate.get("contexts"),
            )
            scores[profile] = row
            profile_rows[profile].append(row)
            profile_outputs[profile].append(output)
        scored_cases.append({"case_id": generated["case_id"], "scores": scores})

    recall = retrieval_recall(candidate_cases, truths)
    metrics = {
        profile: aggregate(
            profile_rows[profile], profile_outputs[profile], pricing=pricing
        )
        for profile in PROFILES
    }
    for profile in PROFILES:
        metrics[profile]["retrieval_recall_at_5"] = recall
    return scored_cases, metrics, gate(metrics, preregistration, thresholds)
