from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.utils.table_pdf_parser import ParsedBlock, html_table_to_markdown


SEMANTIC_SCHEMA = "ecommerce-table-semantic-context-v1"
_TABLE_TYPES = {
    "price": ("价格", "售价", "价目", "price"),
    "inventory_quantity": ("库存", "现货", "inventory", "stock"),
    "delivery_duration": ("配送", "交付", "物流时效", "delivery"),
    "customs_duty_rate": ("关税", "进口税率", "customs", "duty"),
}
_PLATFORM_TERMS = ("天猫", "淘宝", "京东", "拼多多", "抖音", "Amazon", "亚马逊", "eBay", "Shopee", "Lazada", "AliExpress", "速卖通")
_MARKET_TERMS = ("中国", "中国大陆", "美国", "英国", "欧盟", "中国香港", "香港", "日本", "新加坡")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(?:[-/.年](\d{1,2})(?:[-/.月](\d{1,2})日?)?)?(?!\d)")
_CURRENCY_RE = re.compile(r"CNY|RMB|USD|HKD|人民币|美元|港币|[￥¥$]", re.IGNORECASE)
_DURATION_RE = re.compile(r"business\s+days?|hours?|days?|工作日|小时|天|日|时", re.IGNORECASE)


@dataclass(frozen=True)
class PageBlock:
    label: str
    content: str
    bbox: tuple[float, float, float, float] | None
    order: int


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _semantic_value(key: str, value: Any) -> Any:
    if key in {"column_bindings", "table_anchor_bbox", "context_anchor_bbox", "table_bbox"} and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if key in {"binding_source_page", "continuation_from_page"} and value == 0:
        return None
    return value


def semantic_canonical_payload(semantic: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _semantic_value(key, semantic.get(key))
        for key in (
            "schema_version", "table_title", "table_type", "platform", "market",
            "effective_date", "currency", "duration_unit", "column_bindings",
            "binding_source_page", "binding_method", "binding_confidence",
            "continuation_from_page", "table_anchor_bbox", "context_anchor_bbox", "table_bbox",
        )
    }


def semantic_canonical_sha256(semantic: dict[str, Any]) -> str:
    return canonical_sha256(semantic_canonical_payload(semantic))


def semantic_digest_valid(semantic: dict[str, Any]) -> bool:
    return semantic.get("canonical_sha256") == semantic_canonical_sha256(semantic)


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def page_blocks(result_payload: dict[str, Any]) -> list[PageBlock]:
    raw_blocks = result_payload.get("parsing_res_list") or []
    if not isinstance(raw_blocks, list):
        return []
    normalized = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            continue
        bbox = _bbox(raw.get("block_bbox"))
        order = raw.get("block_order") if isinstance(raw.get("block_order"), int) else index + 10_000
        normalized.append(PageBlock(
            label=str(raw.get("block_label", "")).casefold(),
            content=str(raw.get("block_content", "") or "").strip(),
            bbox=bbox,
            order=order,
        ))
    return sorted(normalized, key=lambda item: (item.bbox[1] if item.bbox else float("inf"), item.order))


def _date_text(match: re.Match[str]) -> str:
    year, month, day = match.groups()
    if day:
        return f"{year}-{int(month):02d}-{int(day):02d}"
    if month:
        return f"{year}-{int(month):02d}"
    return year


def _context(text: str) -> dict[str, str]:
    compact = re.sub(r"\s+", "", text)
    table_type = next((kind for kind, aliases in _TABLE_TYPES.items() if any(alias.casefold() in compact.casefold() for alias in aliases)), "")
    platform = next((item for item in _PLATFORM_TERMS if item.casefold() in compact.casefold()), "")
    market = next((item for item in _MARKET_TERMS if item in compact), "")
    date_match = _DATE_RE.search(compact)
    currency_match = _CURRENCY_RE.search(compact)
    duration_match = _DURATION_RE.search(compact)
    currency = ""
    if currency_match:
        raw = currency_match.group(0)
        currency = "CNY" if raw in {"RMB", "人民币", "￥", "¥"} else "USD" if raw in {"美元", "$"} else "HKD" if raw == "港币" else raw.upper()
    duration_unit = ""
    if duration_match:
        raw = duration_match.group(0).casefold()
        duration_unit = "business_day" if "business" in raw or raw == "工作日" else "hour" if raw in {"hour", "hours", "小时", "时"} else "day"
    return {
        "table_type": table_type,
        "platform": platform,
        "market": market,
        "effective_date": _date_text(date_match) if date_match else "",
        "currency": currency,
        "duration_unit": duration_unit,
    }


def _column_bindings(markdown: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("|") or not lines[0].endswith("|"):
        return []
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    bindings = []
    for index, header in enumerate(headers):
        context = _context(header)
        folded = header.casefold()
        fact_type = next((kind for kind, aliases in _TABLE_TYPES.items() if any(alias.casefold() in folded for alias in aliases)), "")
        identity = "sku" if "sku" in folded else "product" if any(term in folded for term in ("商品", "产品", "product")) else ""
        if fact_type or identity or any(context.values()):
            bindings.append({"column_index": index, "header_text": header, "fact_type": fact_type, "identity": identity, **context})
    return bindings


def bind_page_tables(result_payload: dict[str, Any], tables: list[dict[str, Any]], page_number: int) -> list[dict[str, Any]]:
    blocks = page_blocks(result_payload)
    table_blocks = [block for block in blocks if block.label == "table"]
    bound = []
    for index, table in enumerate(tables):
        table_block = table_blocks[index] if index < len(table_blocks) else None
        preceding = []
        if table_block is not None:
            for block in blocks:
                if block is table_block:
                    break
                preceding.append(block)
            previous_tables = [position for position, block in enumerate(preceding) if block.label == "table"]
            if previous_tables:
                preceding = preceding[previous_tables[-1] + 1:]
        anchor = next((block for block in reversed(preceding) if any(_context(block.content).values())), None)
        context = _context(anchor.content) if anchor else {key: "" for key in ("table_type", "platform", "market", "effective_date", "currency", "duration_unit")}
        markdown = html_table_to_markdown(str(table.get("pred_html", "")), str(table.get("ocr_text", "")))
        bindings = _column_bindings(markdown)
        explicit = bool(context["table_type"] or any(binding.get("fact_type") for binding in bindings))
        semantic = {
            "schema_version": SEMANTIC_SCHEMA,
            "table_title": anchor.content.strip() if anchor else "",
            **context,
            "column_bindings": bindings,
            "binding_source_page": page_number if explicit else None,
            "binding_method": "same_page_preceding_anchor" if explicit else "unbound",
            "binding_confidence": "explicit" if explicit else "none",
            "continuation_from_page": None,
            "table_anchor_bbox": list(anchor.bbox) if anchor and anchor.bbox else None,
            "context_anchor_bbox": list(anchor.bbox) if anchor and anchor.bbox else None,
            "table_bbox": list(table_block.bbox) if table_block and table_block.bbox else None,
        }
        semantic["canonical_sha256"] = semantic_canonical_sha256(semantic)
        bound.append(semantic)
    return bound


def semantic_prefix(source: str, page_number: int, table_id: str, semantic: dict[str, Any]) -> str:
    lines = [f"[TableEvidence | source={source} | page={page_number} | table={table_id}]"]
    labels = (
        ("Table", "table_title"), ("Platform", "platform"), ("Market", "market"),
        ("Date", "effective_date"), ("Currency", "currency"), ("Unit", "duration_unit"),
    )
    for label, key in labels:
        if semantic.get(key):
            lines.append(f"{label}: {semantic[key]}")
    bindings = semantic.get("column_bindings") or []
    if bindings:
        values = []
        for binding in bindings:
            details = "/".join(str(binding.get(key, "")) for key in ("identity", "fact_type", "currency", "duration_unit") if binding.get(key))
            values.append(f"c{binding.get('column_index')}={details or binding.get('header_text', '')}")
        lines.append("Columns: " + "; ".join(values))
    return "\n".join(lines)


def _header_signature(block: ParsedBlock) -> str:
    raw = block.metadata.get("column_bindings", "")
    try:
        bindings = json.loads(raw) if isinstance(raw, str) and raw else raw
    except json.JSONDecodeError:
        bindings = []
    identity = [
        {key: binding.get(key, "") for key in ("column_index", "header_text", "identity", "fact_type", "currency", "duration_unit")}
        for binding in (bindings or []) if isinstance(binding, dict) and binding.get("header_text")
    ]
    return canonical_sha256(identity) if identity else ""


def inherit_continuation_context(blocks: Iterable[ParsedBlock]) -> list[ParsedBlock]:
    result = []
    previous_by_source: dict[str, ParsedBlock] = {}
    for block in blocks:
        metadata = dict(block.metadata)
        source = str(metadata.get("source", ""))
        page = metadata.get("page_number")
        previous = previous_by_source.get(source)
        if metadata.get("binding_method") == "unbound" and previous is not None and isinstance(page, int):
            previous_page = previous.metadata.get("page_number")
            if (
                isinstance(previous_page, int) and page == previous_page + 1
                and _header_signature(block) and _header_signature(block) == _header_signature(previous)
                and previous.metadata.get("binding_confidence") in {"explicit", "inherited"}
            ):
                for key in ("table_title", "table_type", "platform", "market", "effective_date", "currency", "duration_unit", "column_bindings"):
                    if previous.metadata.get(key) not in (None, ""):
                        metadata[key] = previous.metadata[key]
                metadata.update({
                    "binding_source_page": previous.metadata.get("binding_source_page", previous_page),
                    "binding_method": "previous_page_continuation",
                    "binding_confidence": "inherited",
                    "continuation_from_page": previous_page,
                })
                semantic = {key: metadata.get(key) for key in semantic_canonical_payload(metadata)}
                metadata["table_semantic_canonical_sha256"] = semantic_canonical_sha256(semantic)
                prefix = semantic_prefix(source, page, str(metadata.get("table_id", "")), semantic)
                block = ParsedBlock(prefix + "\n\n" + str(metadata.get("table_markdown", "") or ""), metadata)
        result.append(ParsedBlock(block.content, metadata))
        if metadata.get("content_type") == "table":
            previous_by_source[source] = result[-1]
    return result
