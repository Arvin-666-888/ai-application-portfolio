import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

from app.schemas.schemas import StructuredAnswer, StructuredFinancialFact, VerificationResult
from app.utils.financial_normalization import (
    financially_equal,
    normalize_financial_value,
    normalize_text,
)
from app.utils.retrieval import enumerate_citation_contexts, parse_query_intent


_NUMERIC_QUESTION_TERMS = (
    "多少", "金额", "收入", "利润", "资产", "负债", "现金流", "比例", "率",
    "%", "百分点", "bp", "bps", "增长", "下降", "增加", "减少",
)
_COMPLEX_FORMULA_TERMS = (
    "计算", "推导", "复合增长", "cagr", "加权", "折现", "现值", "内含报酬率", "公式",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_ANSWER_NUMBER_RE = re.compile(r"(?<![A-Za-z\d])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_ANSWER_CITATION_RE = re.compile(r"\[(C\d+)\]", re.IGNORECASE)
_CURRENCY_IN_TEXT_RE = re.compile(r"CNY|RMB|USD|HKD|人民币|美元|港币", re.IGNORECASE)


@dataclass(frozen=True)
class CitationLedgerEntry:
    citation_id: str
    content: str
    identity: tuple
    content_sha256: str
    source: str
    page_number: Optional[int]
    content_type: Optional[str]


def build_citation_ledger(contexts: list[dict]) -> dict[str, CitationLedgerEntry]:
    """Build the private citation ledger; full content must not be serialized as SourceInfo."""
    ledger = {}
    for citation_id, identity, context in enumerate_citation_contexts(contexts):
        content = str(context.get("content", ""))
        ledger[citation_id] = CitationLedgerEntry(
            citation_id=citation_id,
            content=content,
            identity=identity,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source=str(context.get("source", "")),
            page_number=context.get("page_number"),
            content_type=context.get("content_type"),
        )
    return ledger


def numeric_question_preflight(question: str) -> VerificationResult:
    text = normalize_text(question).casefold()
    if any(term in text for term in _COMPLEX_FORMULA_TERMS):
        return VerificationResult(
            passed=False,
            status="failed",
            errors=["unsupported_complex_formula"],
        )
    if not any(term in text for term in _NUMERIC_QUESTION_TERMS):
        return VerificationResult(passed=True, status="not_applicable")
    return VerificationResult(passed=True, status="passed")


def evidence_preflight(
    question: str,
    citation_ledger: dict[str, CitationLedgerEntry],
) -> VerificationResult:
    question_check = numeric_question_preflight(question)
    if question_check.status != "passed":
        return question_check
    intent = parse_query_intent(question)
    if len(intent.metric_families) > 1:
        return VerificationResult(
            passed=False, status="failed", errors=["unsupported_multi_metric"]
        )
    if not citation_ledger:
        return VerificationResult(
            passed=False, status="failed", errors=["no_fact_binding"]
        )
    for entry in citation_ledger.values():
        for segment in _local_evidence_segments(entry):
            metric_matches = (
                not intent.metric_aliases
                or any(alias in segment for alias in intent.metric_aliases)
            )
            year_matches = not intent.years or any(year in segment for year in intent.years)
            company_matches = (
                not intent.company_terms
                or any(term in segment or term in entry.source for term in intent.company_terms)
            )
            scope_matches = not intent.scopes or any(scope in segment for scope in intent.scopes)
            values = [
                normalize_financial_value(match.group(0))
                for match in re.finditer(
                    r"\(?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?\s*(?:百分点|百万元|千元|万元|亿元|bp|bps|%|元)?",
                    segment,
                    flags=re.IGNORECASE,
                )
                if not re.fullmatch(r"20\d{2}", match.group(0).strip())
            ]
            explicit_values = [
                value for value in values
                if value is not None and value.kind != "number"
            ]
            if (
                metric_matches
                and year_matches
                and company_matches
                and scope_matches
                and explicit_values
            ):
                return VerificationResult(passed=True, status="passed")
    return VerificationResult(
        passed=False, status="failed", errors=["no_fact_binding"]
    )


def parse_structured_output(output: Any) -> StructuredAnswer:
    if isinstance(output, StructuredAnswer):
        return output
    payload = output
    if isinstance(output, str):
        text = output.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        payload = json.loads(text)
    return StructuredAnswer.model_validate(payload)


def _table_preamble(text: str) -> tuple[list[str], dict[int, str], str]:
    preamble = []
    column_labels: dict[int, str] = {}
    unit = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            break
        if stripped.startswith(("Statement:", "Scope:", "Unit:")):
            preamble.append(stripped)
        if stripped.startswith("Unit:"):
            match = re.search(r"(百万元|千元|万元|亿元|元|百分点|bp|bps|%)", stripped, re.IGNORECASE)
            unit = match.group(1) if match else ""
        if stripped.startswith("Columns:"):
            for raw in stripped.removeprefix("Columns:").split(";"):
                match = re.match(r"\s*c(\d+)=(.*)", raw.strip())
                if match:
                    column_labels[int(match.group(1))] = match.group(2).strip()
    return preamble, column_labels, unit


def _markdown_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if len(cells) < 2 or all(not cell or set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    return rows


def _explicit_cell_value(value: str, unit: str) -> str:
    if not unit or not re.fullmatch(r"\(?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?", value.strip()):
        return value
    return value.strip() + unit


def _local_evidence_segments(entry: CitationLedgerEntry) -> list[str]:
    text = normalize_text(entry.content)
    if entry.content_type == "table" and "|" in text:
        rows = _markdown_rows(text)
        if len(rows) >= 2:
            header = rows[0]
            preamble, column_labels, unit = _table_preamble(text)
            materialized = []
            for row in rows[1:]:
                pairs = []
                for index, value in enumerate(row[: len(header)]):
                    if not value:
                        continue
                    label = header[index]
                    binding = column_labels.get(index, "")
                    if binding:
                        label = f"{label}/{binding}"
                    pairs.append(f"{label}={_explicit_cell_value(value, unit if index else '')}")
                if pairs:
                    materialized.append(" | ".join([*preamble, *pairs]))
            if materialized:
                return materialized
    segments = [segment.strip() for segment in _SENTENCE_SPLIT_RE.split(text) if segment.strip()]
    return segments or ([text] if text else [])


def _term_present(term: Optional[str], segment: str, source: str = "") -> bool:
    if not term:
        return True
    normalized = normalize_text(term)
    return normalized in segment or normalized in source


def _fact_supported_in_segment(fact: StructuredFinancialFact, segment: str, source: str) -> bool:
    if not _term_present(fact.metric, segment):
        return False
    if not _term_present(fact.year, segment, source):
        return False
    if not _term_present(fact.company, segment, source):
        return False
    if not _term_present(fact.scope, segment):
        return False

    expected = normalize_financial_value(fact.value_text, fact.unit, fact.currency)
    if expected is None:
        return False
    value_segment = segment
    if fact.year and "=" in segment:
        year_value = re.search(
            rf"{re.escape(fact.year)}年?\s*=\s*([^|]+)", segment
        )
        if year_value:
            value_segment = year_value.group(1)
    declared_currency = _CURRENCY_IN_TEXT_RE.search(value_segment) or _CURRENCY_IN_TEXT_RE.search(segment)
    evidence_currency = declared_currency.group(0) if declared_currency else None
    candidates = []
    for match in re.finditer(
        r"\(?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?\s*(?:百分点|百万元|千元|万元|亿元|bp|bps|%|元)?",
        value_segment,
        flags=re.IGNORECASE,
    ):
        candidate_text = match.group(0).strip()
        if re.fullmatch(r"20\d{2}", candidate_text):
            continue
        actual = normalize_financial_value(candidate_text, currency=evidence_currency)
        if actual is not None:
            candidates.append(actual)
    comparable = [
        actual
        for actual in candidates
        if actual.kind == expected.kind
        and actual.unit == expected.unit
        and actual.currency == expected.currency
    ]
    matching = [actual for actual in comparable if financially_equal(expected, actual)]
    return len(comparable) == 1 and len(matching) == 1


def _question_matches_fact(question: str, fact: StructuredFinancialFact) -> list[str]:
    intent = parse_query_intent(question)
    errors = []
    if intent.years and (not fact.year or fact.year not in intent.years):
        errors.append("question_year_mismatch")
    if intent.company_terms and (
        not fact.company or not any(term in fact.company or fact.company in term for term in intent.company_terms)
    ):
        errors.append("question_company_mismatch")
    if intent.metric_aliases and not any(
        alias in fact.metric or fact.metric in alias for alias in intent.metric_aliases
    ):
        errors.append("question_metric_mismatch")
    if intent.scopes and (not fact.scope or fact.scope not in intent.scopes):
        errors.append("question_scope_mismatch")
    expected_kind = "percent" if intent.answer_type == "ratio" else "amount"
    parsed = normalize_financial_value(fact.value_text, fact.unit, fact.currency)
    if parsed is None:
        errors.append("question_unit_mismatch")
    elif expected_kind == "percent" and parsed.kind not in {"percent", "percentage_point"}:
        errors.append("question_unit_mismatch")
    elif expected_kind == "amount" and parsed.kind != "amount":
        errors.append("question_unit_mismatch")
    return errors


def _answer_has_extra_numbers(answer: StructuredAnswer) -> bool:
    text = re.sub(r"\[?C\d+\]?", "", normalize_text(answer.answer_text), flags=re.IGNORECASE)
    observed = [match.group(0).replace(",", "") for match in _ANSWER_NUMBER_RE.finditer(text)]
    allowed = set()
    for fact in answer.facts:
        parsed = normalize_financial_value(fact.value_text)
        if parsed is not None:
            allowed.add(str(parsed.value))
            allowed.add(format(parsed.value, "f"))
        raw_match = _ANSWER_NUMBER_RE.search(normalize_text(fact.value_text))
        if raw_match:
            allowed.add(raw_match.group(0).replace(",", ""))
        if fact.year:
            allowed.add(normalize_text(fact.year))
    return any(number not in allowed for number in observed)


def verify_structured_answer(
    question: str,
    structured_output: Any,
    citation_ledger: dict[str, CitationLedgerEntry],
) -> VerificationResult:
    preflight = numeric_question_preflight(question)
    if preflight.status != "passed":
        return preflight
    intent = parse_query_intent(question)
    if len(intent.metric_families) > 1:
        return VerificationResult(
            passed=False, status="failed", errors=["unsupported_multi_metric"]
        )
    try:
        answer = parse_structured_output(structured_output)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return VerificationResult(
            passed=False,
            status="failed",
            errors=["invalid_structured_output"],
        )

    errors = []
    verified_ids = []
    answer_citations = list(dict.fromkeys(
        citation.upper() for citation in _ANSWER_CITATION_RE.findall(answer.answer_text)
    ))
    fact_citations = list(dict.fromkeys(
        citation_id
        for fact in answer.facts
        for citation_id in fact.citation_ids
    ))
    unknown_answer_citations = [
        citation_id for citation_id in answer_citations if citation_id not in citation_ledger
    ]
    if unknown_answer_citations:
        errors.append(f"answer_unknown_citation:{','.join(unknown_answer_citations)}")
    if answer.facts and not answer_citations:
        errors.append("answer_missing_citation")
    if set(answer_citations) != set(fact_citations):
        errors.append("answer_fact_citation_mismatch")
    if not answer.facts:
        errors.append("missing_financial_facts")
    if _answer_has_extra_numbers(answer):
        errors.append("answer_contains_uncited_numeric_value")

    for index, fact in enumerate(answer.facts):
        prefix = f"fact_{index + 1}"
        if not fact.citation_ids:
            errors.append(f"{prefix}:missing_citation")
            continue
        unknown = [citation_id for citation_id in fact.citation_ids if citation_id not in citation_ledger]
        if unknown:
            errors.append(f"{prefix}:unknown_citation:{','.join(unknown)}")
            continue
        errors.extend(f"{prefix}:{error}" for error in _question_matches_fact(question, fact))

        supporting_ids = []
        for citation_id in fact.citation_ids:
            entry = citation_ledger[citation_id]
            if any(
                _fact_supported_in_segment(fact, segment, entry.source)
                for segment in _local_evidence_segments(entry)
            ):
                supporting_ids.append(citation_id)
        if not supporting_ids:
            errors.append(f"{prefix}:evidence_mismatch_or_cross_fragment")
        elif len(supporting_ids) != len(fact.citation_ids):
            unsupported = [
                citation_id for citation_id in fact.citation_ids
                if citation_id not in supporting_ids
            ]
            errors.append(f"{prefix}:unsupported_citation:{','.join(unsupported)}")
        else:
            verified_ids.extend(supporting_ids)

    unique_errors = list(dict.fromkeys(errors))
    return VerificationResult(
        passed=not unique_errors,
        status="passed" if not unique_errors else "failed",
        errors=unique_errors,
        verified_citation_ids=list(dict.fromkeys(verified_ids)),
    )


class AnswerVerificationService:
    def build_ledger(self, contexts: list[dict]) -> dict[str, CitationLedgerEntry]:
        return build_citation_ledger(contexts)

    def preflight(
        self,
        question: str,
        contexts: Optional[list[dict]] = None,
        citation_ledger: Optional[dict[str, CitationLedgerEntry]] = None,
    ) -> VerificationResult:
        if contexts is None and citation_ledger is None:
            return numeric_question_preflight(question)
        ledger = citation_ledger if citation_ledger is not None else build_citation_ledger(contexts or [])
        return evidence_preflight(question, ledger)

    def parse(self, output: Any) -> StructuredAnswer:
        return parse_structured_output(output)

    def verify(
        self,
        question: str,
        structured_output: Any,
        contexts: Optional[list[dict]] = None,
        citation_ledger: Optional[dict[str, CitationLedgerEntry]] = None,
    ) -> VerificationResult:
        ledger = citation_ledger if citation_ledger is not None else build_citation_ledger(contexts or [])
        return verify_structured_answer(question, structured_output, ledger)
