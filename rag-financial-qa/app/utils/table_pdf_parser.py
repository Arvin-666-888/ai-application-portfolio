from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping


MAX_FIELD_CHARS = 4_000
MAX_FIELD_BYTES = 8_192
MAX_TABLE_CHARS = 3_000
MAX_TABLE_BYTES = 6_144
MAX_METADATA_BYTES = 16_384


@dataclass(frozen=True)
class ParsedBlock:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexChunk:
    content: str
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass
class _Cell:
    text: str
    is_header: bool
    rowspan: int = 1
    colspan: int = 1


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._parts: list[str] = []
        self.invalid_span = False

    @staticmethod
    def _span(attrs: list[tuple[str, str | None]], name: str) -> int:
        raw = dict(attrs).get(name, "1")
        try:
            value = int(raw or "1")
        except ValueError as exc:
            raise ValueError(f"invalid_{name}") from exc
        if value < 1 or value > 100:
            raise ValueError(f"invalid_{name}")
        return value

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            try:
                rowspan = self._span(attrs, "rowspan")
                colspan = self._span(attrs, "colspan")
            except ValueError:
                self.invalid_span = True
                rowspan = colspan = 1
            self._cell = _Cell("", tag == "th", rowspan, colspan)
            self._parts = []
        elif tag == "br" and self._cell is not None:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._cell.text = "".join(self._parts)
            self._row.append(self._cell)
            self._cell = None
            self._parts = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._parts.append(data)


@dataclass(frozen=True)
class _MarkdownTable:
    markdown: str
    header: list[str]
    data_rows: list[list[str]]
    merged_cells_degraded: bool = False


def _normalize_cell(value: Any) -> str:
    text = unescape(str(value or ""))
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    text = "<br>".join(line for line in lines if line)
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _table_to_markdown(html: str) -> _MarkdownTable:
    parser = _TableHTMLParser()
    parser.feed(html or "")
    parser.close()
    if parser.invalid_span:
        raise ValueError("invalid_merged_cells")
    if not parser.rows:
        raise ValueError("empty_table_html")

    occupied: dict[tuple[int, int], bool] = {}
    grid: list[list[str]] = []
    header_flags: list[list[bool]] = []
    merged = False

    for row_index, source_row in enumerate(parser.rows):
        row: list[str] = []
        flags: list[bool] = []
        column = 0
        for cell in source_row:
            while occupied.get((row_index, column)):
                row.append("")
                flags.append(False)
                column += 1
            if cell.rowspan > 1 or cell.colspan > 1:
                merged = True
            while len(row) <= column:
                row.append("")
                flags.append(False)
            row[column] = _normalize_cell(cell.text)
            flags[column] = cell.is_header
            for row_offset in range(cell.rowspan):
                for col_offset in range(cell.colspan):
                    position = (row_index + row_offset, column + col_offset)
                    if position in occupied:
                        raise ValueError("overlapping_merged_cells")
                    occupied[position] = True
            for _ in range(1, cell.colspan):
                row.append("")
                flags.append(False)
            column += cell.colspan
        grid.append(row)
        header_flags.append(flags)

    width = max(max((len(row) for row in grid), default=0), max((col + 1 for _, col in occupied), default=0))
    if width == 0 or width > 200 or len(grid) > 10_000:
        raise ValueError("unsafe_table_dimensions")
    for row, flags in zip(grid, header_flags):
        row.extend([""] * (width - len(row)))
        flags.extend([False] * (width - len(flags)))

    header_count = 0
    for flags in header_flags:
        if any(flags):
            header_count += 1
        else:
            break

    if header_count:
        header = []
        for column in range(width):
            parts = [grid[row][column] for row in range(header_count) if grid[row][column]]
            header.append(" / ".join(parts))
        data_rows = grid[header_count:]
    else:
        header = [f"Column {index}" for index in range(1, width + 1)]
        data_rows = grid

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in data_rows)
    return _MarkdownTable("\n".join(lines), header, data_rows, merged)


def html_table_to_markdown(html: str, fallback_text: str = "") -> str:
    try:
        return _table_to_markdown(html).markdown
    except (ValueError, TypeError):
        fallback = re.sub(r"\s+", " ", str(fallback_text or "")).strip()
        return fallback


def assess_table_quality(html: str, fallback_text: str = "") -> dict[str, Any]:
    """Return a deterministic, parser-independent quality signal for one table."""
    fallback = re.sub(r"\s+", " ", str(fallback_text or "")).strip()
    try:
        converted = _table_to_markdown(html)
    except (ValueError, TypeError) as exc:
        return {
            "status": "degraded" if fallback else "empty",
            "score": 0.35 if fallback else 0.0,
            "row_count": 0,
            "column_count": 0,
            "nonempty_cell_ratio": 0.0,
            "reason": str(exc)[:200],
        }

    rows = [converted.header, *converted.data_rows]
    cell_count = sum(len(row) for row in rows)
    nonempty_count = sum(bool(str(cell).strip()) for row in rows for cell in row)
    ratio = round(nonempty_count / cell_count, 4) if cell_count else 0.0
    column_count = len(converted.header)
    data_row_count = len(converted.data_rows)
    score = 0.35
    score += 0.25 if column_count >= 2 else 0.1
    score += 0.25 if data_row_count >= 1 else 0.0
    score += 0.15 * ratio
    if converted.merged_cells_degraded:
        score -= 0.05
    score = round(max(0.0, min(score, 1.0)), 4)
    status = "good" if score >= 0.75 else "usable" if score >= 0.45 else "degraded"
    return {
        "status": status,
        "score": score,
        "row_count": data_row_count,
        "column_count": column_count,
        "nonempty_cell_ratio": ratio,
        "reason": "merged_cells_degraded" if converted.merged_cells_degraded else "",
    }


def _truncate_utf8(value: str, max_chars: int, max_bytes: int) -> str:
    value = value[:max_chars]
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _json_size(metadata: Mapping[str, Any]) -> int:
    return len(json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _scalar_value(value: Any) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _truncate_utf8(value, MAX_FIELD_CHARS, MAX_FIELD_BYTES)
    if isinstance(value, (list, tuple, set, dict)):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        except TypeError:
            value = str(value)
    else:
        value = str(value)
    return _truncate_utf8(value, MAX_FIELD_CHARS, MAX_FIELD_BYTES)


def scalarize_metadata(metadata: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    raw = dict(metadata)
    result: dict[str, str | int | float | bool] = {}
    mandatory = (
        "source", "doc_id", "chunk_index", "content_type", "page_number", "physical_page_number",
        "provenance_id", "element_type", "element_id", "parser", "table_id", "table_index",
        "table_chunk_index", "table_chunk_count", "artifact_status", "artifact_schema_version",
        "pdf_sha256", "engine_configuration_fingerprint", "table_content_sha256",
        "table_semantic_schema_version", "table_semantic_canonical_sha256",
        "statement_title", "statement_type", "table_scope", "unit_text", "unit", "currency",
        "statement_period", "column_bindings", "binding_source_page", "binding_method",
        "binding_confidence", "continuation_from_page",
    )

    for key in mandatory:
        if key in raw:
            scalar = _scalar_value(raw.pop(key))
            if scalar is not None:
                result[key] = scalar

    while _json_size(result) > MAX_METADATA_BYTES:
        string_keys = [key for key, value in result.items() if isinstance(value, str) and value]
        if not string_keys:
            raise ValueError("mandatory metadata exceeds MAX_METADATA_BYTES")
        longest = max(string_keys, key=lambda key: len(str(result[key]).encode("utf-8")))
        value = str(result[longest])
        result[longest] = value[:max(0, len(value) // 2)]

    def add_group(group: dict[str, str | int | float | bool]) -> bool:
        candidate = dict(result)
        candidate.update(group)
        if _json_size(candidate) > MAX_METADATA_BYTES:
            return False
        result.update(group)
        return True

    table_values: dict[str, str] = {}
    for key in ("table_markdown", "table_html"):
        value = raw.pop(key, None)
        if not value:
            continue
        text = str(value)
        chars = len(text)
        byte_count = len(text.encode("utf-8"))
        if chars <= MAX_TABLE_CHARS and byte_count <= MAX_TABLE_BYTES:
            table_values[key] = text
        else:
            add_group({
                f"{key}_omitted": True,
                f"{key}_chars": chars,
                f"{key}_bytes": byte_count,
                f"{key}_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })

    for key in sorted(raw):
        scalar = _scalar_value(raw[key])
        if scalar is not None:
            add_group({str(key): scalar})

    for key in ("table_markdown", "table_html"):
        if key not in table_values:
            continue
        text = table_values[key]
        if not add_group({key: text}):
            add_group({
                f"{key}_omitted": True,
                f"{key}_chars": len(text),
                f"{key}_bytes": len(text.encode("utf-8")),
                f"{key}_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })

    if _json_size(result) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds MAX_METADATA_BYTES")
    return result


class TablePDFParser:
    def __init__(
        self,
        partitioner: Callable[..., list[Any]] | None = None,
        *,
        use_hi_res: bool = False,
    ) -> None:
        self._partitioner = partitioner
        self.use_hi_res = use_hi_res

    @property
    def profile(self) -> str:
        return "unstructured-hi-res-v2" if self.use_hi_res else "unstructured-fast-v1"

    def _get_partitioner(self) -> Callable[..., list[Any]]:
        if self._partitioner is not None:
            return self._partitioner
        try:
            from unstructured.partition.pdf import partition_pdf
        except ImportError as exc:
            raise RuntimeError("缺少 unstructured[pdf]，请在项目独立虚拟环境中安装") from exc
        return partition_pdf

    def parse(
        self,
        file_path: str | Path,
        *,
        doc_id: int,
        source: str,
        physical_page_number: int | None = None,
    ) -> list[ParsedBlock]:
        if physical_page_number is not None and (
            isinstance(physical_page_number, bool)
            or not isinstance(physical_page_number, int)
            or physical_page_number < 1
        ):
            raise ValueError("physical_page_number must be a positive integer")
        partitioner = self._get_partitioner()
        signature = inspect.signature(partitioner)
        parameters = signature.parameters
        if "strategy" not in parameters or "infer_table_structure" not in parameters:
            raise RuntimeError("当前 partition_pdf 版本不支持已验证的 PDF 解析参数")

        strategy = "hi_res" if self.use_hi_res else "fast"
        try:
            elements = partitioner(
                filename=str(file_path),
                strategy=strategy,
                infer_table_structure=self.use_hi_res,
                languages=["chi_sim", "eng"],
            )
        except Exception as exc:
            raise RuntimeError(f"partition_pdf {strategy} 解析失败: {exc}") from exc

        normalized: list[dict[str, Any]] = []
        for ordinal, element in enumerate(elements):
            metadata_obj = getattr(element, "metadata", None)
            parsed_page_number = int(getattr(metadata_obj, "page_number", 0) or 0)
            page_number = physical_page_number if physical_page_number is not None else parsed_page_number
            category = str(getattr(element, "category", type(element).__name__) or type(element).__name__)
            text = str(getattr(element, "text", "") or "").strip()
            element_id = str(getattr(element, "id", "") or f"element_{ordinal}")
            table_html = str(getattr(metadata_obj, "text_as_html", "") or "")
            normalized.append({
                "ordinal": ordinal,
                "page_number": page_number,
                "parsed_page_number": parsed_page_number,
                "category": category,
                "text": text,
                "element_id": element_id,
                "table_html": table_html,
                "is_table": category.lower() == "table" or type(element).__name__.lower() == "table",
            })

        blocks: list[ParsedBlock] = []
        table_index = 0
        for index, item in enumerate(normalized):
            page_number = item["page_number"]
            provenance_id = f"doc_{doc_id}:page_{page_number}:element_{item['ordinal']}"
            base_metadata: dict[str, Any] = {
                "source": source,
                "doc_id": doc_id,
                "page_number": page_number,
                "element_type": item["category"],
                "element_id": item["element_id"],
                "provenance_id": provenance_id,
                "parser": self.profile,
            }
            if physical_page_number is not None:
                base_metadata.update({
                    "physical_page_number": physical_page_number,
                    "parser_page_number": item["parsed_page_number"],
                    "single_page_mapping": True,
                })
            if not item["is_table"]:
                if item["text"]:
                    base_metadata["content_type"] = "text"
                    blocks.append(ParsedBlock(item["text"], base_metadata))
                continue

            table_index += 1
            table_id = f"doc_{doc_id}:page_{page_number}:table_{table_index}"
            conversion = "html"
            conversion_reason = ""
            merged = False
            try:
                converted = _table_to_markdown(item["table_html"])
                markdown = converted.markdown
                merged = converted.merged_cells_degraded
            except (ValueError, TypeError) as exc:
                markdown = re.sub(r"\s+", " ", item["text"]).strip()
                conversion = "fallback"
                conversion_reason = str(exc)[:200]
            if not markdown:
                markdown = f"[Table on page {page_number}: no extractable cells]"
                conversion = "placeholder"
                conversion_reason = conversion_reason or "empty_table"

            before = self._nearest_context(normalized, index, page_number, -1)
            after = self._nearest_context(normalized, index, page_number, 1)
            context_lines = [f"[Table | source={source} | page={page_number}]"]
            if before:
                context_lines.append(f"Context before: {before}")
            if after:
                context_lines.append(f"Context after: {after}")
            content = "\n".join(context_lines) + "\n\n" + markdown
            quality = assess_table_quality(item["table_html"], item["text"])
            table_metadata = {
                **base_metadata,
                "content_type": "table",
                "table_id": table_id,
                "table_index": table_index,
                "table_html": item["table_html"],
                "table_markdown": markdown,
                "table_conversion": conversion,
                "merged_cells_degraded": merged,
                "table_quality_status": quality["status"],
                "table_quality_score": quality["score"],
                "table_row_count": quality["row_count"],
                "table_column_count": quality["column_count"],
                "table_nonempty_cell_ratio": quality["nonempty_cell_ratio"],
                "context_before": before,
                "context_after": after,
            }
            if conversion_reason:
                table_metadata["table_conversion_reason"] = conversion_reason
            blocks.append(ParsedBlock(content, table_metadata))

        if not blocks:
            raise ValueError("PDF 文件内容为空或无法提取文本及表格")
        return blocks

    def parse_page(
        self,
        file_path: str | Path,
        *,
        doc_id: int,
        source: str,
        physical_page_number: int,
    ) -> list[ParsedBlock]:
        """Parse a single-page PDF and remap parser-local page numbers to the source PDF."""
        return self.parse(
            file_path,
            doc_id=doc_id,
            source=source,
            physical_page_number=physical_page_number,
        )

    @staticmethod
    def _nearest_context(
        elements: list[dict[str, Any]], current: int, page_number: int, direction: int,
    ) -> str:
        index = current + direction
        while 0 <= index < len(elements):
            candidate = elements[index]
            if candidate["page_number"] != page_number:
                break
            if not candidate["is_table"] and candidate["text"]:
                return _truncate_utf8(candidate["text"], 200, 800)
            index += direction
        return ""


def _fixed_windows(content: str, hard_limit: int) -> list[str]:
    return [content[index:index + hard_limit] for index in range(0, len(content), hard_limit)]


def _split_table_content(
    block: ParsedBlock,
    soft_limit: int,
    hard_limit: int,
    table_row_overlap: int = 0,
) -> list[str]:
    if table_row_overlap not in (0, 1):
        raise ValueError("table_row_overlap must be 0 or 1")
    markdown = str(block.metadata.get("table_markdown", "") or "")
    if not markdown or "\n" not in markdown:
        return _fixed_windows(block.content, hard_limit)

    lines = markdown.splitlines()
    if len(lines) < 2:
        return _fixed_windows(block.content, hard_limit)
    header = lines[:2]
    data_rows = lines[2:]
    prefix = block.content.split("\n\n", 1)[0]
    base = prefix + "\n\n" + "\n".join(header)
    if len(base) >= hard_limit:
        return _fixed_windows(block.content, hard_limit)
    if not data_rows:
        return [base]

    chunks: list[str] = []
    current: list[str] = []

    def render(rows: list[str]) -> str:
        return base + "\n" + "\n".join(rows)

    for row in data_rows:
        if len(base) + 1 + len(row) > hard_limit:
            if current:
                chunks.append(render(current))
                current = []
            available = hard_limit - len(base) - 1
            for start in range(0, len(row), available):
                chunks.append(base + "\n" + row[start:start + available])
            continue

        candidate_rows = current + [row]
        if current and len(render(candidate_rows)) > soft_limit:
            chunks.append(render(current))
            overlap_rows = current[-1:] if table_row_overlap else []
            if overlap_rows and len(render(overlap_rows + [row])) > hard_limit:
                overlap_rows = []
            current = overlap_rows
        current.append(row)
    if current:
        chunks.append(render(current))
    if not all(0 < len(chunk) <= hard_limit for chunk in chunks):
        raise ValueError("table chunk exceeds hard limit")
    return chunks


def build_index_chunks(
    blocks: list[ParsedBlock],
    splitter: Any,
    table_row_overlap: int = 0,
) -> list[IndexChunk]:
    if table_row_overlap not in (0, 1):
        raise ValueError("table_row_overlap must be 0 or 1")
    chunks: list[IndexChunk] = []
    soft_limit = max(int(getattr(splitter, "chunk_size", 400)), 600)
    hard_limit = max(int(getattr(splitter, "chunk_size", 400)) * 2, 1_200)

    for block in blocks:
        content_type = block.metadata.get("content_type", "text")
        if content_type == "table":
            contents = _split_table_content(
                block, soft_limit, hard_limit, table_row_overlap
            )
        else:
            contents = splitter.split_text(block.content)
        count = len(contents)
        for local_index, content in enumerate(contents):
            metadata = dict(block.metadata)
            if content_type == "table":
                metadata["table_chunk_index"] = local_index
                metadata["table_chunk_count"] = count
            chunks.append(IndexChunk(content.strip(), scalarize_metadata(metadata)))

    return [chunk for chunk in chunks if chunk.content]
