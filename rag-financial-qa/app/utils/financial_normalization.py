import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


_CURRENCY_ALIASES = {
    "CNY": "CNY",
    "RMB": "CNY",
    "人民币": "CNY",
    "元": "CNY",
    "￥": "CNY",
    "¥": "CNY",
    "USD": "USD",
    "美元": "USD",
    "$": "USD",
    "HKD": "HKD",
    "港币": "HKD",
    "HK$": "HKD",
}
_DURATION_UNIT_ALIASES = {
    "小时": "hour",
    "时": "hour",
    "hour": "hour",
    "hours": "hour",
    "天": "day",
    "日": "day",
    "day": "day",
    "days": "day",
    "工作日": "business_day",
    "business day": "business_day",
    "business days": "business_day",
    "business_day": "business_day",
}
_NUMBER_RE = re.compile(
    r"(?P<negative>\()?\s*(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?(negative)\))"
)
_CURRENCY_RE = re.compile(
    r"HK\$|CNY|RMB|USD|HKD|人民币|美元|港币|元|[￥¥$]",
    re.IGNORECASE,
)
_DURATION_UNIT_RE = re.compile(
    r"business\s+days?|hours?|days?|个?工作日|小时|天|日|时",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"%|％")
_UNSUPPORTED_SCALE_RE = re.compile(r"(?<=\d)\s*(?:[kK]|千|万|百万|亿)(?![\w])")


@dataclass(frozen=True)
class NormalizedEcommerceValue:
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
    normalized = normalize_text(value)
    return _CURRENCY_ALIASES.get(normalized.upper()) or _CURRENCY_ALIASES.get(normalized)


def _canonical_duration_unit(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", normalize_text(value)).casefold()
    normalized = normalized.removeprefix("个")
    return _DURATION_UNIT_ALIASES.get(normalized)


def _embedded_currency(text: str) -> Optional[str]:
    match = _CURRENCY_RE.search(text)
    return _canonical_currency(match.group(0)) if match else None


def _embedded_duration_unit(text: str) -> Optional[str]:
    match = _DURATION_UNIT_RE.search(text)
    return _canonical_duration_unit(match.group(0)) if match else None


def normalize_ecommerce_value(
    value_text: str,
    unit: Optional[str] = None,
    currency: Optional[str] = None,
    *,
    fact_type: Optional[str] = None,
) -> Optional[NormalizedEcommerceValue]:
    """Normalize one explicit supported ecommerce value without guessing units."""
    text = normalize_text(value_text)
    match = _NUMBER_RE.search(text)
    if not match or _UNSUPPORTED_SCALE_RE.search(text):
        return None
    try:
        value = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    if match.group("negative"):
        value = -abs(value)

    declared_currency = _canonical_currency(currency)
    embedded_currency = _embedded_currency(text)
    if currency is not None and declared_currency is None:
        return None
    if declared_currency and embedded_currency and declared_currency != embedded_currency:
        return None
    explicit_currency = declared_currency or embedded_currency

    declared_unit = _canonical_duration_unit(unit)
    embedded_unit = _embedded_duration_unit(text[match.end():])
    if unit is not None and normalize_text(unit) not in {"%", "％", "percent"} and declared_unit is None:
        return None
    if declared_unit and embedded_unit and declared_unit != embedded_unit:
        return None
    explicit_unit = declared_unit or embedded_unit
    has_percent = bool(_PERCENT_RE.search(text)) or normalize_text(unit or "") in {"%", "％", "percent"}

    if fact_type == "price":
        if value < 0 or explicit_currency is None or explicit_unit is not None or has_percent:
            return None
        return NormalizedEcommerceValue(value, value, None, explicit_currency, "price")
    if fact_type == "inventory_quantity":
        if value < 0 or value != value.to_integral_value() or explicit_currency or explicit_unit or has_percent:
            return None
        return NormalizedEcommerceValue(value, value, None, None, "inventory_quantity")
    if fact_type == "delivery_duration":
        if value < 0 or explicit_unit is None or explicit_currency or has_percent:
            return None
        canonical = value * Decimal("24") if explicit_unit == "day" else value
        return NormalizedEcommerceValue(value, canonical, explicit_unit, None, "delivery_duration")
    if fact_type == "customs_duty_rate":
        if value < 0 or not has_percent or explicit_currency or explicit_unit:
            return None
        return NormalizedEcommerceValue(value, value, "percent", None, "customs_duty_rate")
    if fact_type is not None:
        return None

    if value < 0:
        return None
    if explicit_currency is not None and explicit_unit is None and not has_percent:
        return NormalizedEcommerceValue(value, value, None, explicit_currency, "price")
    if has_percent and explicit_currency is None and explicit_unit is None:
        return NormalizedEcommerceValue(value, value, "percent", None, "customs_duty_rate")
    if explicit_unit is not None and explicit_currency is None:
        return NormalizedEcommerceValue(value, value, explicit_unit, None, "delivery_duration")
    if value == value.to_integral_value() and explicit_currency is None:
        return NormalizedEcommerceValue(value, value, None, None, "inventory_quantity")
    return None


def extract_ecommerce_values(
    text: object,
    *,
    fact_type: Optional[str] = None,
) -> list[NormalizedEcommerceValue]:
    normalized = normalize_text(text)
    values = []
    for match in _NUMBER_RE.finditer(normalized):
        start = max(0, match.start() - 16)
        end = min(len(normalized), match.end() + 24)
        parsed = normalize_ecommerce_value(normalized[start:end], fact_type=fact_type)
        if parsed is not None:
            values.append(parsed)
    return values


def ecommerce_values_equal(
    left: NormalizedEcommerceValue,
    right: NormalizedEcommerceValue,
) -> bool:
    same_duration_scale = (
        left.kind == right.kind == "delivery_duration"
        and left.unit in {"hour", "day"}
        and right.unit in {"hour", "day"}
    )
    return (
        left.kind == right.kind
        and (left.unit == right.unit or same_duration_scale)
        and left.currency == right.currency
        and left.canonical_value == right.canonical_value
    )
