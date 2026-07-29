import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


_SCALE_FACTORS = {
    "元": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "百万元": Decimal("1000000"),
    "亿元": Decimal("100000000"),
}
_CURRENCY_ALIASES = {
    "CNY": "CNY",
    "RMB": "CNY",
    "人民币": "CNY",
    "USD": "USD",
    "美元": "USD",
    "HKD": "HKD",
    "港币": "HKD",
}
_NUMBER_RE = re.compile(r"(?P<negative>\()?\s*(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?(negative)\))")
_UNIT_RE = re.compile(r"百分点|百万元|千元|万元|亿元|bp|bps|%|元", re.IGNORECASE)
_CURRENCY_RE = re.compile(r"CNY|RMB|USD|HKD|人民币|美元|港币", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedFinancialValue:
    value: Decimal
    canonical_value: Decimal
    unit: Optional[str]
    currency: Optional[str]
    kind: str


def normalize_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip()


def _canonical_currency(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = normalize_text(value).upper()
    return _CURRENCY_ALIASES.get(normalized) or _CURRENCY_ALIASES.get(normalize_text(value))


def normalize_financial_value(
    value_text: str,
    unit: Optional[str] = None,
    currency: Optional[str] = None,
) -> Optional[NormalizedFinancialValue]:
    """Normalize one explicit financial value without inferring missing units."""
    text = normalize_text(value_text)
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        value = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    if match.group("negative"):
        value = -abs(value)

    explicit_unit = normalize_text(unit) if unit else None
    if explicit_unit is None:
        unit_match = _UNIT_RE.search(text[match.end():])
        explicit_unit = unit_match.group(0) if unit_match else None
    if explicit_unit:
        lowered_unit = explicit_unit.casefold()
        if lowered_unit in {"bp", "bps"}:
            return NormalizedFinancialValue(
                value=value,
                canonical_value=value / Decimal("100"),
                unit="percentage_point",
                currency=None,
                kind="percentage_point",
            )
        if explicit_unit == "百分点":
            return NormalizedFinancialValue(
                value=value,
                canonical_value=value,
                unit="percentage_point",
                currency=None,
                kind="percentage_point",
            )
        if explicit_unit == "%":
            return NormalizedFinancialValue(
                value=value,
                canonical_value=value,
                unit="percent",
                currency=None,
                kind="percent",
            )

    explicit_currency = _canonical_currency(currency)
    if explicit_currency is None:
        currency_match = _CURRENCY_RE.search(text)
        explicit_currency = _canonical_currency(currency_match.group(0)) if currency_match else None

    if explicit_unit in _SCALE_FACTORS:
        return NormalizedFinancialValue(
            value=value,
            canonical_value=value * _SCALE_FACTORS[explicit_unit],
            unit="元",
            currency=explicit_currency,
            kind="amount",
        )
    return NormalizedFinancialValue(
        value=value,
        canonical_value=value,
        unit=None,
        currency=explicit_currency,
        kind="number",
    )


def extract_financial_values(text: object) -> list[NormalizedFinancialValue]:
    normalized = normalize_text(text)
    values = []
    for match in _NUMBER_RE.finditer(normalized):
        start = max(0, match.start() - 12)
        end = min(len(normalized), match.end() + 12)
        parsed = normalize_financial_value(normalized[start:end])
        if parsed is not None:
            values.append(parsed)
    return values


def financially_equal(
    left: NormalizedFinancialValue,
    right: NormalizedFinancialValue,
) -> bool:
    if left.kind != right.kind:
        return False
    if left.unit != right.unit:
        return False
    if left.currency != right.currency:
        return False
    return left.canonical_value == right.canonical_value
