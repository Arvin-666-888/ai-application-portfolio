from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.utils.table_pdf_parser import ParsedBlock, html_table_to_markdown


SEMANTIC_SCHEMA = "financial-table-semantic-context-v1"
_STATEMENT_TYPES = ("资产负债表", "利润表", "现金流量表", "所有者权益变动表")
_UNIT_RE = re.compile(r"(?:金额)?单位(?:为|：|:)?\s*(人民币)?\s*(元|千元|万元|百万元|亿元)")
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?:年|年度)?(?!\d)")


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
    if key in {"column_bindings", "statement_anchor_bbox", "unit_anchor_bbox", "table_bbox"} and isinstance(value, str):
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
            "schema_version", "statement_title", "statement_type", "table_scope", "unit_text",
            "unit", "currency", "statement_period", "column_bindings", "binding_source_page",
            "binding_method", "binding_confidence", "continuation_from_page", "statement_anchor_bbox",
            "unit_anchor_bbox", "table_bbox",
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
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return values  # type: ignore[return-value]


def page_blocks(result_payload: dict[str, Any]) -> list[PageBlock]:
    raw_blocks = result_payload.get("parsing_res_list") or []
    if not isinstance(raw_blocks, list):
        return []
    normalized = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            continue
        bbox = _bbox(raw.get("block_bbox"))
        explicit_order = raw.get("block_order")
        order = int(explicit_order) if isinstance(explicit_order, int) else index + 10_000
        normalized.append(PageBlock(
            label=str(raw.get("block_label", "")).casefold(),
            content=str(raw.get("block_content", "") or "").strip(),
            bbox=bbox,
            order=order,
        ))
    return sorted(
        normalized,
        key=lambda item: (
            item.bbox[1] if item.bbox else float("inf"),
            item.order,
        ),
    )


def _statement_context(text: str) -> tuple[str, str, str]:
    compact = re.sub(r"\s+", "", text)
    statement_type = next((item for item in _STATEMENT_TYPES if item in compact), "")
    if not statement_type:
        return "", "", ""
    if "合并及公司" in compact or "合并及母公司" in compact:
        scope = "column-bound"
    elif "母公司" in compact or "公司利润表" in compact:
        scope = "母公司"
    elif "合并" in compact or "集团" in compact:
        scope = "合并"
    else:
        scope = ""
    return text.strip(), statement_type, scope


def _column_bindings(markdown: str, default_scope: str, unit: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("|") or not lines[0].endswith("|"):
        return []
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    if all(re.fullmatch(r"Column \d+", header) for header in headers) and len(lines) >= 3:
        semantic_headers = [cell.strip() for cell in lines[2].strip("|").split("|")]
        if len(semantic_headers) == len(headers) and any(
            _YEAR_RE.search(header) or "合并" in header or "公司" in header
            for header in semantic_headers
        ):
            headers = semantic_headers
    bindings = []
    for index, header in enumerate(headers):
        year_match = _YEAR_RE.search(header)
        if "合并" in header or "集团" in header:
            scope = "合并"
        elif "母公司" in header or "公司" in header:
            scope = "母公司"
        else:
            scope = "" if default_scope == "column-bound" else default_scope
        if year_match or scope or (unit and index > 0):
            bindings.append({
                "column_index": index,
                "header_text": header,
                "year": year_match.group(1) if year_match else "",
                "scope": scope,
                "unit": unit,
            })
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

        statement_title = statement_type = table_scope = ""
        unit_text = currency = unit = statement_period = ""
        statement_anchor = None
        unit_anchor = None
        for block in preceding:
            title, kind, scope = _statement_context(block.content)
            if kind:
                statement_title, statement_type, table_scope = title, kind, scope
                statement_anchor = block
            unit_match = _UNIT_RE.search(block.content)
            if unit_match:
                unit_text = unit_match.group(0)
                currency = "CNY" if unit_match.group(1) else ""
                unit = unit_match.group(2)
                unit_anchor = block
        year_match = _YEAR_RE.search(statement_title)
        if year_match:
            statement_period = year_match.group(1)

        markdown = html_table_to_markdown(
            str(table.get("pred_html", "")), str(table.get("ocr_text", ""))
        )
        bindings = _column_bindings(markdown, table_scope, unit)
        explicit = bool(statement_type and (unit or any(item["unit"] for item in bindings)))
        semantic = {
            "schema_version": SEMANTIC_SCHEMA,
            "statement_title": statement_title,
            "statement_type": statement_type,
            "table_scope": table_scope,
            "unit_text": unit_text,
            "unit": unit,
            "currency": currency,
            "statement_period": statement_period,
            "column_bindings": bindings,
            "binding_source_page": page_number if explicit else None,
            "binding_method": "same_page_preceding_anchor" if explicit else "unbound",
            "binding_confidence": "explicit" if explicit else "none",
            "continuation_from_page": None,
            "statement_anchor_bbox": list(statement_anchor.bbox) if statement_anchor and statement_anchor.bbox else None,
            "unit_anchor_bbox": list(unit_anchor.bbox) if unit_anchor and unit_anchor.bbox else None,
            "table_bbox": list(table_block.bbox) if table_block and table_block.bbox else None,
        }
        semantic["canonical_sha256"] = semantic_canonical_sha256(semantic)
        bound.append(semantic)
    return bound


def semantic_prefix(source: str, page_number: int, table_id: str, semantic: dict[str, Any]) -> str:
    lines = [f"[TableEvidence | source={source} | page={page_number} | table={table_id}]"]
    if semantic.get("statement_title"):
        lines.append(f"Statement: {semantic['statement_title']}")
    if semantic.get("table_scope") and semantic.get("table_scope") != "column-bound":
        lines.append(f"Scope: {semantic['table_scope']}")
    if semantic.get("unit_text"):
        lines.append(f"Unit: {semantic['unit_text']}")
    bindings = semantic.get("column_bindings") or []
    if bindings:
        values = []
        for binding in bindings:
            details = "/".join(
                str(binding.get(key, "")) for key in ("year", "scope", "unit")
                if binding.get(key)
            )
            values.append(f"c{binding.get('column_index')}={details or binding.get('header_text', '')}")
        lines.append("Columns: " + "; ".join(values))
    return "\n".join(lines)


def _header_signature(block: ParsedBlock) -> str:
    raw_bindings = block.metadata.get("column_bindings", "")
    if isinstance(raw_bindings, str):
        try:
            bindings = json.loads(raw_bindings) if raw_bindings else []
        except json.JSONDecodeError:
            bindings = []
    else:
        bindings = raw_bindings if isinstance(raw_bindings, list) else []
    header_identity = [
        {
            "column_index": binding.get("column_index"),
            "header_text": _YEAR_RE.sub("YEAR", re.sub(r"\s+", "", str(binding.get("header_text", "")))),
            "scope": binding.get("scope", ""),
            "unit": binding.get("unit", ""),
        }
        for binding in bindings
        if isinstance(binding, dict) and binding.get("header_text")
    ]
    if not header_identity:
        return ""
    return canonical_sha256(header_identity)


def inherit_continuation_context(blocks: Iterable[ParsedBlock]) -> list[ParsedBlock]:
    result = []
    previous_by_source: dict[str, ParsedBlock] = {}
    for block in blocks:
        metadata = dict(block.metadata)
        source = str(metadata.get("source", ""))
        page = metadata.get("page_number")
        current_method = str(metadata.get("binding_method", ""))
        previous = previous_by_source.get(source)
        if current_method == "unbound" and previous is not None and isinstance(page, int):
            previous_page = previous.metadata.get("page_number")
            if (
                isinstance(previous_page, int)
                and page == previous_page + 1
                and _header_signature(block)
                and _header_signature(block) == _header_signature(previous)
                and previous.metadata.get("binding_confidence") in {"explicit", "inherited"}
            ):
                for key in (
                    "statement_title", "statement_type", "table_scope", "unit_text", "unit",
                    "currency", "statement_period", "column_bindings",
                ):
                    if previous.metadata.get(key) not in (None, ""):
                        metadata[key] = previous.metadata[key]
                metadata.update({
                    "binding_source_page": previous.metadata.get("binding_source_page", previous_page),
                    "binding_method": "previous_page_continuation",
                    "binding_confidence": "inherited",
                    "continuation_from_page": previous_page,
                })
                semantic_identity = {
                    key: metadata.get(key)
                    for key in (
                        "statement_title", "statement_type", "table_scope", "unit_text", "unit",
                        "currency", "statement_period", "column_bindings", "binding_source_page",
                        "binding_method", "binding_confidence", "continuation_from_page",
                    )
                }
                metadata["table_semantic_canonical_sha256"] = canonical_sha256(semantic_identity)
                prefix = semantic_prefix(
                    source, page, str(metadata.get("table_id", "")), semantic_identity
                )
                markdown = str(metadata.get("table_markdown", "") or "")
                block = ParsedBlock(prefix + "\n\n" + markdown, metadata)
        result.append(ParsedBlock(block.content, metadata))
        if metadata.get("content_type") == "table":
            previous_by_source[source] = result[-1]
    return result
