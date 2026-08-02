import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

from app.schemas.schemas import StructuredAnswer, StructuredEcommerceFact, VerificationResult
from app.utils.financial_normalization import (
    ecommerce_values_equal,
    normalize_ecommerce_value,
    normalize_text,
)
from app.utils.retrieval import enumerate_citation_contexts, parse_query_intent


# MIGRATION: 财务数值核验 -> 仅核验四类电商事实，并保持 Citation Ledger fail-closed。
_SUPPORTED_FACT_TERMS = {
    "price": ("价格", "售价", "单价", "多少钱", "price"),
    "inventory_quantity": ("库存数量", "现货数量", "库存", "inventory quantity", "stock quantity", "inventory", "stock"),
    "delivery_duration": ("配送时长", "交付时长", "配送时间", "送达时间", "物流时效", "delivery"),
    "customs_duty_rate": ("关税税率", "关税率", "进口税率", "customs duty", "duty rate"),
}
_UNSUPPORTED_SPEC_TERMS = (
    "重量", "尺寸", "长宽高", "功率", "电压", "电流", "容量", "规格", "瓦", "伏",
    "weight", "dimension", "watt", "voltage", "ampere",
)
_COMPLEX_FORMULA_TERMS = ("计算", "推导", "换算", "估算", "预测", "加权", "公式")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_ANSWER_NUMBER_RE = re.compile(r"(?<![A-Za-z\d])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_ANSWER_CITATION_RE = re.compile(r"\[(C\d+)\]", re.IGNORECASE)
_VALUE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?:HK\$|CNY|RMB|USD|HKD|人民币|美元|港币|元|[￥¥$])?\s*"
    r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:HK\$|CNY|RMB|USD|HKD|人民币|美元|港币|元|"
    r"business\s+days?|hours?|days?|个?工作日|小时|天|日|时|%|％|[kK]|千|万|百万|亿)?"
    r"(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)


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
    if any(term in text for term in _UNSUPPORTED_SPEC_TERMS):
        return VerificationResult(passed=False, status="failed", errors=["unsupported_fact_type"])
    if any(term in text for term in _COMPLEX_FORMULA_TERMS):
        return VerificationResult(passed=False, status="failed", errors=["unsupported_complex_formula"])
    intent = parse_query_intent(question)
    if not intent.fact_types:
        return VerificationResult(passed=False, status="failed", errors=["unsupported_fact_type"])
    if len(intent.fact_types) > 1:
        return VerificationResult(passed=False, status="failed", errors=["unsupported_multi_fact"])
    return VerificationResult(passed=True, status="passed")


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


def _table_preamble(text: str) -> list[str]:
    labels = ("Table:", "Platform:", "Market:", "Date:", "Currency:", "Unit:", "Columns:")
    return [line.strip() for line in text.splitlines() if line.strip().startswith(labels)]


def _local_evidence_segments(entry: CitationLedgerEntry) -> list[str]:
    text = normalize_text(entry.content)
    if entry.content_type == "table" and "|" in text:
        rows = _markdown_rows(text)
        if len(rows) >= 2:
            header = rows[0]
            preamble = _table_preamble(text)
            materialized = []
            for row in rows[1:]:
                pairs = [
                    f"{header[index]}={value}"
                    for index, value in enumerate(row[: len(header)])
                    if value
                ]
                if pairs:
                    materialized.append(" | ".join([*preamble, *pairs]))
            if materialized:
                return materialized
    segments = [segment.strip() for segment in _SENTENCE_SPLIT_RE.split(text) if segment.strip()]
    return segments or ([text] if text else [])


def _canonical_dates(text: str) -> set[str]:
    values = set()
    for match in re.finditer(
        r"(?<!\d)(20\d{2})(?:[-/.年](\d{1,2})(?:[-/.月](\d{1,2})日?)?)?(?!\d)",
        normalize_text(text),
    ):
        year, month, day = match.groups()
        values.add(
            f"{year}-{int(month):02d}-{int(day):02d}" if day
            else f"{year}-{int(month):02d}" if month
            else year
        )
    return values


def _market_aliases(value: str) -> set[str]:
    folded = normalize_text(value).casefold()
    groups = (
        {"香港", "中国香港", "hk", "hong kong"},
        {"美国", "us", "usa", "united states"},
        {"英国", "uk", "united kingdom"},
        {"欧盟", "eu", "european union"},
        {"新加坡", "sg", "singapore"},
        {"中国", "中国大陆", "cn", "china"},
    )
    for group in groups:
        if folded in group:
            return group
    return {folded}


def _term_present(
    term: Optional[str],
    segment: str,
    source: str = "",
    *,
    identifier: bool = False,
    date: bool = False,
    market: bool = False,
) -> bool:
    if not term:
        return True
    if date:
        expected = _canonical_dates(term)
        return bool(expected and expected <= _canonical_dates(segment))
    if market:
        folded = f"{segment} {source}".casefold()
        return any(
            re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", folded)
            if re.search(r"[A-Za-z]", alias)
            else alias in folded
            for alias in _market_aliases(term)
        )
    normalized = normalize_text(term).casefold()
    if identifier:
        return bool(re.search(rf"(?<![A-Za-z0-9._-]){re.escape(normalized)}(?![A-Za-z0-9._-])", segment.casefold()))
    return normalized in segment.casefold() or normalized in source.casefold()


def _fact_alias_present(fact_type: str, segment: str) -> bool:
    folded = segment.casefold()
    if fact_type == "inventory_quantity" and any(
        term in folded
        for term in ("库存费", "库存周转", "inventory fee", "inventory turnover")
    ):
        return False
    return any(alias.casefold() in folded for alias in _SUPPORTED_FACT_TERMS[fact_type])


def _identity_supported(fact: StructuredEcommerceFact, segment: str, source: str) -> bool:
    if not _term_present(fact.sku, segment, identifier=True):
        return False
    for term in (fact.product, fact.platform):
        if not _term_present(term, segment, source):
            return False
    if not _term_present(fact.market, segment, source, market=True):
        return False
    return _term_present(fact.date, segment, date=True)


def _candidate_values(segment: str, fact_type: str) -> list:
    sanitized = re.sub(
        r"(?i)SKU\s*(?:编号|编码|ID)?\s*[:：#-]?\s*[A-Z0-9][A-Z0-9._-]{1,63}",
        " ",
        segment,
    )
    sanitized = re.sub(
        r"(?<!\d)20\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?(?!\d)",
        " ",
        sanitized,
    )
    values = []
    for match in _VALUE_TOKEN_RE.finditer(sanitized):
        token = match.group(0).strip()
        if not token:
            continue
        parsed = normalize_ecommerce_value(token, fact_type=fact_type)
        if parsed is not None:
            values.append(parsed)
    return values


def _fact_supported_in_segment(fact: StructuredEcommerceFact, segment: str, source: str) -> bool:
    if not _fact_alias_present(fact.fact_type, segment):
        return False
    if not _identity_supported(fact, segment, source):
        return False
    expected = normalize_ecommerce_value(
        fact.value_text,
        fact.unit,
        fact.currency,
        fact_type=fact.fact_type,
    )
    if expected is None:
        return False
    comparable = _candidate_values(segment, fact.fact_type)
    matching = [actual for actual in comparable if ecommerce_values_equal(expected, actual)]
    return len(comparable) == 1 and len(matching) == 1


def evidence_preflight(question: str, citation_ledger: dict[str, CitationLedgerEntry]) -> VerificationResult:
    question_check = numeric_question_preflight(question)
    if question_check.status != "passed":
        return question_check
    intent = parse_query_intent(question)
    fact_type = intent.fact_types[0]
    if not citation_ledger:
        return VerificationResult(passed=False, status="failed", errors=["no_fact_binding"])
    for entry in citation_ledger.values():
        for segment in _local_evidence_segments(entry):
            if not _fact_alias_present(fact_type, segment):
                continue
            identity_checks = [
                *(_term_present(term, segment, entry.source) for term in intent.product_terms),
                *(_term_present(term, segment, entry.source) for term in intent.platforms),
                *(_term_present(term, segment, entry.source, market=True) for term in intent.markets),
                *(_term_present(term, segment, identifier=True) for term in intent.skus),
                *(_term_present(term, segment, date=True) for term in intent.dates),
            ]
            if identity_checks and not all(identity_checks):
                continue
            if len(_candidate_values(segment, fact_type)) == 1:
                return VerificationResult(passed=True, status="passed")
    return VerificationResult(passed=False, status="failed", errors=["no_fact_binding"])


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


def _question_matches_fact(question: str, fact: StructuredEcommerceFact) -> list[str]:
    intent = parse_query_intent(question)
    errors = []
    if intent.fact_types and fact.fact_type not in intent.fact_types:
        errors.append("question_fact_type_mismatch")
    fields = (
        ("sku", intent.skus, True, False, False),
        ("product", intent.product_terms, False, False, False),
        ("platform", intent.platforms, False, False, False),
        ("market", intent.markets, False, False, True),
        ("date", intent.dates, False, True, False),
    )
    for name, expected, identifier, date_field, market_field in fields:
        actual = getattr(fact, name)
        if expected and (
            not actual
            or not any(
                _term_present(
                    term,
                    actual,
                    identifier=identifier,
                    date=date_field,
                    market=market_field,
                )
                and _term_present(
                    actual,
                    term,
                    identifier=identifier,
                    date=date_field,
                    market=market_field,
                )
                for term in expected
            )
        ):
            errors.append(f"question_{name}_mismatch")
    parsed = normalize_ecommerce_value(fact.value_text, fact.unit, fact.currency, fact_type=fact.fact_type)
    if parsed is None:
        errors.append("question_unit_mismatch")
    return errors


def _answer_has_extra_numbers(answer: StructuredAnswer) -> bool:
    text = re.sub(r"\[?C\d+\]?", "", normalize_text(answer.answer_text), flags=re.IGNORECASE)
    observed = [match.group(0).replace(",", "") for match in _ANSWER_NUMBER_RE.finditer(text)]
    allowed = set()
    for fact in answer.facts:
        raw = _ANSWER_NUMBER_RE.search(normalize_text(fact.value_text))
        if raw:
            allowed.add(raw.group(0).replace(",", ""))
        for identity in (fact.sku, fact.date):
            if identity:
                allowed.update(match.group(0).replace(",", "") for match in _ANSWER_NUMBER_RE.finditer(identity))
    return any(number not in allowed for number in observed)


def verify_structured_answer(
    question: str,
    structured_output: Any,
    citation_ledger: dict[str, CitationLedgerEntry],
) -> VerificationResult:
    preflight = numeric_question_preflight(question)
    if preflight.status != "passed":
        return preflight
    try:
        answer = parse_structured_output(structured_output)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return VerificationResult(passed=False, status="failed", errors=["invalid_structured_output"])

    errors = []
    verified_ids = []
    answer_citations = list(dict.fromkeys(citation.upper() for citation in _ANSWER_CITATION_RE.findall(answer.answer_text)))
    fact_citations = list(dict.fromkeys(cid for fact in answer.facts for cid in fact.citation_ids))
    unknown_answer = [cid for cid in answer_citations if cid not in citation_ledger]
    if unknown_answer:
        errors.append(f"answer_unknown_citation:{','.join(unknown_answer)}")
    if answer.facts and not answer_citations:
        errors.append("answer_missing_citation")
    if set(answer_citations) != set(fact_citations):
        errors.append("answer_fact_citation_mismatch")
    if not answer.facts:
        errors.append("missing_ecommerce_facts")
    if _answer_has_extra_numbers(answer):
        errors.append("answer_contains_uncited_numeric_value")

    fact_values: dict[tuple, set[tuple]] = {}
    for fact in answer.facts:
        identity = (
            fact.fact_type,
            normalize_text(fact.sku or "").casefold(),
            normalize_text(fact.product or "").casefold(),
            normalize_text(fact.platform or "").casefold(),
            normalize_text(fact.market or "").casefold(),
            tuple(sorted(_canonical_dates(fact.date or ""))),
        )
        parsed = normalize_ecommerce_value(
            fact.value_text, fact.unit, fact.currency, fact_type=fact.fact_type
        )
        if parsed is not None:
            fact_values.setdefault(identity, set()).add(
                (parsed.canonical_value, parsed.unit, parsed.currency)
            )
    if any(len(values) > 1 for values in fact_values.values()):
        errors.append("conflicting_ecommerce_facts")

    for index, fact in enumerate(answer.facts):
        prefix = f"fact_{index + 1}"
        if not fact.citation_ids:
            errors.append(f"{prefix}:missing_citation")
            continue
        unknown = [cid for cid in fact.citation_ids if cid not in citation_ledger]
        if unknown:
            errors.append(f"{prefix}:unknown_citation:{','.join(unknown)}")
            continue
        errors.extend(f"{prefix}:{error}" for error in _question_matches_fact(question, fact))
        supporting = []
        for citation_id in fact.citation_ids:
            entry = citation_ledger[citation_id]
            if any(_fact_supported_in_segment(fact, segment, entry.source) for segment in _local_evidence_segments(entry)):
                supporting.append(citation_id)
        if not supporting:
            errors.append(f"{prefix}:evidence_mismatch_or_cross_fragment")
        elif len(supporting) != len(fact.citation_ids):
            unsupported = [cid for cid in fact.citation_ids if cid not in supporting]
            errors.append(f"{prefix}:unsupported_citation:{','.join(unsupported)}")
        else:
            verified_ids.extend(supporting)

    unique = list(dict.fromkeys(errors))
    return VerificationResult(
        passed=not unique,
        status="passed" if not unique else "failed",
        errors=unique,
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
