from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from app.utils.table_pdf_parser import (
    ParsedBlock,
    html_table_to_markdown,
    scalarize_metadata,
)


PDF_PAGE_PROBE_SCHEMA = "pdf-page-probe-v1"
PDF_PAGE_ROUTE_SCHEMA = "pdf-page-route-v1"
PDF_PARSE_RESULT_SCHEMA = "pdf-parse-result-v1"
PDF_ROUTING_POLICY_VERSION = "ecommerce-pdf-routing-v1"

# MIGRATION: 历史金融候选词仍由冻结 eval/artifact 保存；活动路由只补商品手册、关税合规与物流记录候选词。
TABLE_TITLES = (
    "商品价格表", "商品目录", "商品手册", "SKU清单", "库存清单", "库存表", "平台价目表",
    "关税合规", "关税合规记录", "配送时效", "物流时效", "物流记录", "交付时长", "关税税率表", "进口税率表",
    "Price List", "Product Manual", "Inventory", "Delivery Time", "Logistics Record", "Customs Compliance", "Customs Duty",
)
METRIC_TERMS = (
    "SKU", "商品", "产品", "商品手册", "价格", "售价", "库存", "现货", "配送时长",
    "物流记录", "交付时长", "工作日", "关税合规", "关税税率", "进口税率", "price", "inventory",
    "delivery", "logistics", "customs compliance", "customs duty",
)
UNIT_TERMS = (
    "CNY", "RMB", "USD", "HKD", "人民币", "美元", "港币", "￥", "¥", "$",
    "小时", "天", "工作日", "%", "％",
)
PERIOD_TERMS = ("生效日期", "更新日期", "截至", "Date", "Effective")
YEAR_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
CHINESE_PATTERN = re.compile(r"[㐀-䶿一-鿿]")
DIGIT_PATTERN = re.compile(r"\d")

ROUTING_POLICY: dict[str, Any] = {
    "policy_version": PDF_ROUTING_POLICY_VERSION,
    "probe_schema": PDF_PAGE_PROBE_SCHEMA,
    "route_schema": PDF_PAGE_ROUTE_SCHEMA,
    "external_labels_used": False,
    "title_neighbor_range": {"before": 1, "after": 3},
    "numeric_ratio_min": 0.18,
    "line_count_min": 15,
    "low_text_min_chars": 20,
    "max_pages": 80,
    "requires_metric_term": True,
    "requires_explicit_date_or_context": False,
    "cap_priority": "title_hit,numeric_ratio_desc,page_number_asc",
    "layers": ("pdfplumber-embedded-text", "injected-hi-res-page", "injected-artifact"),
}


def _fingerprint(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_effective_policy(
    *,
    max_pages: int = 80,
    numeric_ratio_min: float = 0.18,
    line_count_min: int = 15,
    low_text_min_chars: int = 20,
    title_neighbor_before: int = 1,
    title_neighbor_after: int = 3,
) -> dict[str, Any]:
    if max_pages < 1:
        raise ValueError("max_pages must be greater than zero")
    if not 0.0 <= numeric_ratio_min <= 1.0:
        raise ValueError("numeric_ratio_min must be between zero and one")
    if line_count_min < 1 or low_text_min_chars < 0:
        raise ValueError("line_count_min must be positive and low_text_min_chars non-negative")
    if title_neighbor_before < 0 or title_neighbor_after < 0:
        raise ValueError("title neighbor ranges must be non-negative")
    return {
        **ROUTING_POLICY,
        "numeric_ratio_min": numeric_ratio_min,
        "line_count_min": line_count_min,
        "low_text_min_chars": low_text_min_chars,
        "title_neighbor_range": {
            "before": title_neighbor_before,
            "after": title_neighbor_after,
        },
        "max_pages": max_pages,
    }


POLICY_FINGERPRINT = _fingerprint(ROUTING_POLICY)
Scalar = str | int | float | bool


@dataclass(frozen=True)
class PDFPageProbe:
    source: str
    page_number: int
    text: str = field(repr=False)
    text_chars: int = 0
    chinese_chars: int = 0
    digit_chars: int = 0
    numeric_ratio: float = 0.0
    line_count: int = 0
    table_title_hits: tuple[str, ...] = ()
    metric_hits: tuple[str, ...] = ()
    year_hits: tuple[str, ...] = ()
    period_hits: tuple[str, ...] = ()
    unit_hits: tuple[str, ...] = ()
    empty_text: bool = True
    low_text: bool = True
    extraction_error: str = ""

    def feature_metadata(
        self,
        *,
        policy_fingerprint: str = POLICY_FINGERPRINT,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Scalar]:
        return scalarize_metadata({
            "schema_version": PDF_PAGE_PROBE_SCHEMA,
            "source": self.source,
            "page_number": self.page_number,
            "text_chars": self.text_chars,
            "chinese_chars": self.chinese_chars,
            "digit_chars": self.digit_chars,
            "numeric_ratio": self.numeric_ratio,
            "line_count": self.line_count,
            "table_title_hits": self.table_title_hits,
            "metric_hits": self.metric_hits,
            "year_hits": self.year_hits,
            "period_hits": self.period_hits,
            "unit_hits": self.unit_hits,
            "empty_text": self.empty_text,
            "low_text": self.low_text,
            "extraction_error": self.extraction_error,
            "policy_fingerprint": policy_fingerprint,
            "policy_version": (policy or ROUTING_POLICY)["policy_version"],
        })


@dataclass(frozen=True)
class PDFPageRoute:
    source: str
    page_number: int
    reasons: tuple[str, ...]
    selected: bool
    dropped_by_cap: bool = False
    l2_attempted: bool = False
    l3_attempted: bool = False
    l2_status: str = "disabled"
    l3_status: str = "disabled"
    table_layer: str = ""
    table_count: int = 0
    degraded: bool = False
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Scalar] = field(default_factory=dict)


@dataclass(frozen=True)
class PDFParseResult:
    status: str
    blocks: tuple[ParsedBlock, ...]
    page_routes: tuple[PDFPageRoute, ...]
    warnings: tuple[str, ...]
    page_count: int
    selected_page_count: int
    dropped_page_count: int
    policy_fingerprint: str = POLICY_FINGERPRINT
    schema_version: str = PDF_PARSE_RESULT_SCHEMA


class PageParser(Protocol):
    def __call__(
        self,
        file_path: str | Path,
        page_number: int,
        *,
        doc_id: int,
        source: str,
    ) -> Any: ...


class PDFOpener(Protocol):
    def __call__(self, file_path: str | Path) -> Any: ...


def _hits(text: str, terms: Iterable[str]) -> tuple[str, ...]:
    folded = text.casefold()
    return tuple(term for term in terms if term.casefold() in folded)


def page_features(
    text: str,
    source: str,
    page_number: int,
    *,
    extraction_error: str = "",
    policy: Mapping[str, Any] | None = None,
) -> PDFPageProbe:
    """Build bounded routing features from one physical page without external labels."""
    if page_number < 1:
        raise ValueError("page_number must be a positive physical page number")
    text = text or ""
    compact = "".join(text.split())
    text_chars = len(compact)
    digit_chars = len(DIGIT_PATTERN.findall(compact))
    effective_policy = policy or ROUTING_POLICY
    low_text = text_chars < int(effective_policy["low_text_min_chars"])
    return PDFPageProbe(
        source=source,
        page_number=page_number,
        text=text,
        text_chars=text_chars,
        chinese_chars=len(CHINESE_PATTERN.findall(compact)),
        digit_chars=digit_chars,
        numeric_ratio=round(digit_chars / max(text_chars, 1), 6),
        line_count=sum(bool(line.strip()) for line in text.splitlines()),
        table_title_hits=_hits(text, TABLE_TITLES),
        metric_hits=_hits(text, METRIC_TERMS),
        year_hits=tuple(sorted(set(YEAR_PATTERN.findall(text)))),
        period_hits=_hits(text, PERIOD_TERMS),
        unit_hits=_hits(text, UNIT_TERMS),
        empty_text=not bool(compact),
        low_text=low_text,
        extraction_error=extraction_error,
    )


def _value(page: PDFPageProbe | Mapping[str, Any], name: str, default: Any) -> Any:
    return getattr(page, name) if isinstance(page, PDFPageProbe) else page.get(name, default)


def classify(
    page: PDFPageProbe | Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Classify one page using only deterministic page features."""
    effective_policy = policy or ROUTING_POLICY
    reasons: list[str] = []
    if _value(page, "table_title_hits", ()):
        reasons.append("ecommerce_table_title")

    if (
        float(_value(page, "numeric_ratio", 0.0)) >= float(effective_policy["numeric_ratio_min"])
        and int(_value(page, "line_count", 0)) >= int(effective_policy["line_count_min"])
        and bool(_value(page, "metric_hits", ()))
        and bool(_value(page, "unit_hits", ()))
    ):
        reasons.append("numeric_ecommerce_page")
    if bool(_value(page, "low_text", False)):
        reasons.append("low_text")
    return tuple(reasons)


classify_page = classify


def _route_metadata(
    route: PDFPageRoute,
    *,
    policy_fingerprint: str = POLICY_FINGERPRINT,
    pdf_sha256: str = "",
) -> dict[str, Scalar]:
    selected_layer = route.table_layer or "L1"
    route_path = "L1"
    if route.l2_attempted:
        route_path += "->L2"
    if route.l3_attempted:
        route_path += "->L3"
    fallback_reason = next(
        (warning for warning in route.warnings if warning.startswith(("l2_", "l3_"))),
        "",
    )
    return scalarize_metadata({
        "schema_version": PDF_PAGE_ROUTE_SCHEMA,
        "source": route.source,
        "pdf_sha256": pdf_sha256,
        "page_number": route.page_number,
        "route_reasons": route.reasons,
        "candidate_reasons": route.reasons,
        "selected": route.selected,
        "dropped_by_cap": route.dropped_by_cap,
        "l2_attempted": route.l2_attempted,
        "l3_attempted": route.l3_attempted,
        "l2_status": route.l2_status,
        "l3_status": route.l3_status,
        "table_layer": route.table_layer,
        "selected_layer": selected_layer,
        "route_path": route_path,
        "fallback_from": "L2" if route.l3_attempted and route.l2_attempted else "",
        "fallback_reason": fallback_reason,
        "parse_status": "degraded" if route.degraded else "succeeded",
        "table_count": route.table_count,
        "degraded": route.degraded,
        "warnings": route.warnings,
        "policy_version": PDF_ROUTING_POLICY_VERSION,
        "policy_fingerprint": policy_fingerprint,
    })


def _with_metadata(
    route: PDFPageRoute,
    *,
    policy_fingerprint: str = POLICY_FINGERPRINT,
    pdf_sha256: str = "",
) -> PDFPageRoute:
    return replace(
        route,
        metadata=_route_metadata(
            route,
            policy_fingerprint=policy_fingerprint,
            pdf_sha256=pdf_sha256,
        ),
    )


def select(
    pages: Sequence[PDFPageProbe],
    max_pages: int = 80,
    *,
    policy: Mapping[str, Any] | None = None,
    policy_fingerprint: str = POLICY_FINGERPRINT,
    pdf_sha256: str = "",
) -> tuple[PDFPageRoute, ...]:
    """Select and cap L2/L3 pages; output remains ordered by physical page."""
    if max_pages < 1:
        raise ValueError("max_pages must be greater than zero")
    effective_policy = policy or ROUTING_POLICY
    if len({page.page_number for page in pages}) != len(pages):
        raise ValueError("page_number values must be unique")

    by_number = {page.page_number: page for page in pages}
    candidates: dict[int, set[str]] = {}
    title_pages: list[int] = []
    for page in pages:
        reasons = classify(page, effective_policy)
        if reasons:
            candidates.setdefault(page.page_number, set()).update(reasons)
        if "ecommerce_table_title" in reasons:
            title_pages.append(page.page_number)

    before = int(effective_policy["title_neighbor_range"]["before"])
    after = int(effective_policy["title_neighbor_range"]["after"])
    for title_page in title_pages:
        for page_number in range(title_page - before, title_page + after + 1):
            if page_number in by_number:
                candidates.setdefault(page_number, set()).add("title_neighbor")

    ranked = sorted(
        candidates,
        key=lambda page_number: (
            0 if "ecommerce_table_title" in candidates[page_number] else 1,
            -by_number[page_number].numeric_ratio,
            page_number,
        ),
    )
    selected = set(ranked[:max_pages])
    dropped = set(ranked[max_pages:])
    routes = []
    for page in sorted(pages, key=lambda item: item.page_number):
        route = PDFPageRoute(
            source=page.source,
            page_number=page.page_number,
            reasons=tuple(sorted(candidates.get(page.page_number, ()))),
            selected=page.page_number in selected,
            dropped_by_cap=page.page_number in dropped,
        )
        routes.append(_with_metadata(
            route,
            policy_fingerprint=policy_fingerprint,
            pdf_sha256=pdf_sha256,
        ))
    return tuple(routes)


select_pages = select


def _default_pdf_opener(file_path: str | Path) -> Any:
    import pdfplumber

    return pdfplumber.open(file_path)


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _call_page_parser(
    parser: PageParser | Any,
    file_path: str | Path,
    page_number: int,
    doc_id: int,
    source: str,
    pdf_sha256: str,
) -> Any:
    target = parser
    if not callable(target):
        target = getattr(parser, "parse_page", None)
    if not callable(target):
        raise TypeError("injected page parser must be callable or expose parse_page")
    parameters = inspect.signature(target).parameters
    accepts_extra = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {"doc_id": doc_id, "source": source}
    if "pdf_sha256" in parameters or accepts_extra:
        kwargs["pdf_sha256"] = pdf_sha256
    return target(file_path, page_number, **kwargs)


def _is_placeholder(content: str, metadata: Mapping[str, Any]) -> bool:
    conversion = str(metadata.get("table_conversion", "")).lower()
    normalized = re.sub(r"\s+", " ", content or "").strip().lower()
    markers = (
        "no extractable cells",
        "[table: no extractable cells]",
        "[table on page",
    )
    return (
        not normalized
        or conversion == "placeholder"
        or any(marker in normalized for marker in markers)
    )


def _mapping_table(mapping: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    metadata_value = mapping.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    explicit_type = str(metadata.get("content_type", mapping.get("content_type", ""))).lower()
    has_table_fields = any(
        key in mapping for key in ("pred_html", "ocr_text", "table_markdown", "table_html")
    )
    if explicit_type and explicit_type != "table" and not has_table_fields:
        return None

    markdown = str(mapping.get("table_markdown", metadata.get("table_markdown", "")) or "").strip()
    html = str(mapping.get("pred_html", mapping.get("table_html", metadata.get("table_html", ""))) or "")
    fallback = str(mapping.get("ocr_text", mapping.get("content", "")) or "").strip()
    if not markdown and html:
        markdown = html_table_to_markdown(html, fallback).strip()
    content = markdown or fallback
    metadata.update({key: value for key, value in mapping.items() if key not in {"metadata", "content"}})
    metadata["content_type"] = "table"
    if markdown:
        metadata["table_markdown"] = markdown
    if html:
        metadata["table_html"] = html
    return content, metadata


def _table_candidates(value: Any) -> list[tuple[str, dict[str, Any]]]:
    if value is None:
        return []
    result_blocks = getattr(value, "blocks", None)
    if result_blocks is not None and result_blocks is not value:
        return _table_candidates(result_blocks)
    if isinstance(value, ParsedBlock):
        if str(value.metadata.get("content_type", "")).lower() != "table":
            return []
        return [(value.content, dict(value.metadata))]
    if isinstance(value, str):
        return [(value, {"content_type": "table"})]
    if isinstance(value, Mapping):
        tables = value.get("tables")
        if isinstance(tables, Sequence) and not isinstance(tables, (str, bytes)):
            candidates: list[tuple[str, dict[str, Any]]] = []
            for table in tables:
                candidates.extend(_table_candidates(table))
            return candidates
        projected = _mapping_table(value)
        return [projected] if projected is not None else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        candidates = []
        for item in value:
            candidates.extend(_table_candidates(item))
        return candidates
    return []


def _normalize_tables(
    value: Any,
    *,
    layer: str,
    doc_id: int,
    source: str,
    page_number: int,
    pdf_sha256: str,
    policy_fingerprint: str,
    candidate_reasons: Sequence[str],
    fallback_from: str = "",
    fallback_reason: str = "",
    l2_attempted: bool = False,
) -> tuple[ParsedBlock, ...]:
    blocks: list[ParsedBlock] = []
    for raw_index, (content, raw_metadata) in enumerate(_table_candidates(value), 1):
        if _is_placeholder(content, raw_metadata):
            continue
        table_index = len(blocks) + 1
        table_id = f"doc_{doc_id}:page_{page_number}:table_{table_index}"
        provenance_id = f"doc_{doc_id}:page_{page_number}:{layer.lower()}:table_{raw_index}"
        upstream_provenance = raw_metadata.get("provenance_id", "")
        metadata = {
            **raw_metadata,
            "source": source,
            "pdf_sha256": pdf_sha256,
            "doc_id": doc_id,
            "page_number": page_number,
            "content_type": "table",
            "parser_layer": layer,
            "selected_layer": layer,
            "route_path": (
                "L1->L2" if layer == "L2"
                else ("L1->L2->L3" if l2_attempted else "L1->L3")
            ),
            "fallback_from": fallback_from,
            "fallback_reason": fallback_reason,
            "parse_status": "succeeded",
            "candidate_reasons": tuple(candidate_reasons),
            "table_id": table_id,
            "table_index": table_index,
            "provenance_id": provenance_id,
            "upstream_provenance_id": upstream_provenance,
            "policy_fingerprint": policy_fingerprint,
        }
        prefix = f"[Table | source={source} | page={page_number}]"
        normalized_content = content.strip()
        if not normalized_content.startswith("[Table |"):
            normalized_content = f"{prefix}\n\n{normalized_content}"
        blocks.append(ParsedBlock(normalized_content, scalarize_metadata(metadata)))
    return tuple(blocks)


class PDFParseRouter:
    def __init__(
        self,
        hi_res_page_parser: PageParser | Any | None = None,
        artifact_adapter: PageParser | Any | None = None,
        *,
        max_pages: int = 80,
        numeric_ratio_min: float = 0.18,
        line_count_min: int = 15,
        low_text_min_chars: int = 20,
        title_neighbor_before: int = 1,
        title_neighbor_after: int = 3,
        pdf_opener: PDFOpener | None = None,
    ) -> None:
        self.hi_res_page_parser = hi_res_page_parser
        self.artifact_adapter = artifact_adapter
        self.max_pages = max_pages
        self.policy = build_effective_policy(
            max_pages=max_pages,
            numeric_ratio_min=numeric_ratio_min,
            line_count_min=line_count_min,
            low_text_min_chars=low_text_min_chars,
            title_neighbor_before=title_neighbor_before,
            title_neighbor_after=title_neighbor_after,
        )
        self.policy_fingerprint = _fingerprint(self.policy)
        self._pdf_opener = pdf_opener or _default_pdf_opener

    def parse(
        self,
        file_path: str | Path,
        *,
        doc_id: int,
        source: str | None = None,
    ) -> PDFParseResult:
        path = Path(file_path)
        source = source or path.name
        pdf_sha256 = _sha256_file(path)
        probes: list[PDFPageProbe] = []
        warnings: list[str] = []

        with self._pdf_opener(path) as document:
            for page_number, page in enumerate(document.pages, 1):
                extraction_error = ""
                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    text = ""
                    extraction_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    warnings.append(f"l1_extract_failed:p{page_number}:{extraction_error}")
                probes.append(
                    page_features(
                        text,
                        source,
                        page_number,
                        extraction_error=extraction_error,
                        policy=self.policy,
                    )
                )

        routes = list(select(
            probes,
            self.max_pages,
            policy=self.policy,
            policy_fingerprint=self.policy_fingerprint,
            pdf_sha256=pdf_sha256,
        ))
        dropped_count = sum(route.dropped_by_cap for route in routes)
        if dropped_count:
            warning = f"page_cap_dropped:{dropped_count}:max_pages={self.max_pages}"
            warnings.append(warning)
            routes = [
                _with_metadata(
                    replace(route, warnings=route.warnings + (warning,)),
                    policy_fingerprint=self.policy_fingerprint,
                    pdf_sha256=pdf_sha256,
                )
                if route.dropped_by_cap else route
                for route in routes
            ]

        blocks: list[ParsedBlock] = []
        for probe in probes:
            if probe.text.strip():
                provenance_id = f"doc_{doc_id}:page_{probe.page_number}:l1:text"
                blocks.append(ParsedBlock(
                    probe.text.strip(),
                    scalarize_metadata({
                        "source": source,
                        "pdf_sha256": pdf_sha256,
                        "doc_id": doc_id,
                        "page_number": probe.page_number,
                        "content_type": "text",
                        "element_type": "PageText",
                        "parser": "pdfplumber-embedded-text-v1",
                        "parser_layer": "L1",
                        "selected_layer": "L1",
                        "route_path": "L1",
                        "fallback_from": "",
                        "fallback_reason": "",
                        "parse_status": "succeeded",
                        "candidate_reasons": classify(probe, self.policy),
                        "provenance_id": provenance_id,
                        "policy_fingerprint": self.policy_fingerprint,
                        "page_features": probe.feature_metadata(
                            policy_fingerprint=self.policy_fingerprint,
                            policy=self.policy,
                        ),
                    }),
                ))

        route_by_page = {route.page_number: index for index, route in enumerate(routes)}
        for probe in probes:
            route_index = route_by_page[probe.page_number]
            route = routes[route_index]
            if not route.selected:
                continue

            page_warnings = list(route.warnings)
            table_blocks: tuple[ParsedBlock, ...] = ()
            l2_attempted = self.hi_res_page_parser is not None
            l3_attempted = False
            l2_status = "disabled"
            l3_status = "disabled"
            fallback_reason = ""

            if self.hi_res_page_parser is not None:
                l2_status = "attempted"
                try:
                    raw = _call_page_parser(
                        self.hi_res_page_parser,
                        path,
                        probe.page_number,
                        doc_id,
                        source,
                        pdf_sha256,
                    )
                    table_blocks = _normalize_tables(
                        raw,
                        layer="L2",
                        doc_id=doc_id,
                        source=source,
                        page_number=probe.page_number,
                        pdf_sha256=pdf_sha256,
                        policy_fingerprint=self.policy_fingerprint,
                        candidate_reasons=route.reasons,
                    )
                    if table_blocks:
                        l2_status = "succeeded"
                    else:
                        l2_status = "empty"
                        fallback_reason = f"l2_no_valid_table:p{probe.page_number}"
                        page_warnings.append(fallback_reason)
                except Exception as exc:
                    l2_status = "failed"
                    fallback_reason = (
                        f"l2_failed:p{probe.page_number}:{type(exc).__name__}:{str(exc)[:160]}"
                    )
                    page_warnings.append(fallback_reason)

            if not table_blocks and self.artifact_adapter is not None:
                l3_attempted = True
                l3_status = "attempted"
                try:
                    raw = _call_page_parser(
                        self.artifact_adapter,
                        path,
                        probe.page_number,
                        doc_id,
                        source,
                        pdf_sha256,
                    )
                    table_blocks = _normalize_tables(
                        raw,
                        layer="L3",
                        doc_id=doc_id,
                        source=source,
                        page_number=probe.page_number,
                        pdf_sha256=pdf_sha256,
                        policy_fingerprint=self.policy_fingerprint,
                        candidate_reasons=route.reasons,
                        fallback_from="L2" if l2_attempted else "",
                        fallback_reason=fallback_reason if l2_attempted else "",
                        l2_attempted=l2_attempted,
                    )
                    if table_blocks:
                        l3_status = "succeeded"
                    else:
                        l3_status = "empty"
                        page_warnings.append(f"l3_no_valid_table:p{probe.page_number}")
                except Exception as exc:
                    l3_status = "failed"
                    page_warnings.append(
                        f"l3_failed:p{probe.page_number}:{type(exc).__name__}:{str(exc)[:160]}"
                    )

            table_layer = str(table_blocks[0].metadata.get("parser_layer", "")) if table_blocks else ""
            degraded = not bool(table_blocks)
            if degraded:
                page_warnings.append(f"candidate_no_valid_table:p{probe.page_number}")
            if degraded and probe.text.strip():
                page_warnings.append(f"l1_preserved_degraded:p{probe.page_number}")
            warnings.extend(item for item in page_warnings if item not in warnings)
            blocks.extend(table_blocks)
            routes[route_index] = _with_metadata(
                replace(
                    route,
                    l2_attempted=l2_attempted,
                    l3_attempted=l3_attempted,
                    l2_status=l2_status,
                    l3_status=l3_status,
                    table_layer=table_layer,
                    table_count=len(table_blocks),
                    degraded=degraded,
                    warnings=tuple(page_warnings),
                ),
                policy_fingerprint=self.policy_fingerprint,
                pdf_sha256=pdf_sha256,
            )

        if not blocks:
            status = "failed"
            warnings.append("all_layers_empty")
        elif any(route.degraded for route in routes):
            status = "degraded"
        else:
            status = "succeeded"

        return PDFParseResult(
            status=status,
            blocks=tuple(blocks),
            page_routes=tuple(routes),
            warnings=tuple(warnings),
            page_count=len(probes),
            selected_page_count=sum(route.selected for route in routes),
            dropped_page_count=dropped_count,
            policy_fingerprint=self.policy_fingerprint,
        )
