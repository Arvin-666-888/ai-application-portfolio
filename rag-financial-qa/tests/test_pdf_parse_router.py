from __future__ import annotations

from app.utils.pdf_parse_router import (
    POLICY_FINGERPRINT,
    PDFPageProbe,
    PDFParseRouter,
    ROUTING_POLICY,
    classify,
    page_features,
    select,
)
from app.utils.table_pdf_parser import ParsedBlock


class _Page:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self) -> str:
        return self.text


class _PDF:
    def __init__(self, texts: list[str]):
        self.pages = [_Page(text) for text in texts]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _pdf_path(tmp_path, content: bytes = b"synthetic-pdf"):
    path = tmp_path / "report.pdf"
    path.write_bytes(content)
    return path


def _probe(page_number: int, text: str) -> PDFPageProbe:
    return page_features(text, "report.pdf", page_number)


def _financial_page(title: str = "") -> str:
    rows = [title, "SKU 商品 价格 库存", "SKU-A100 背包 USD 100 库存 90"]
    rows.extend(f"SKU-{i:03d} 商品{i} USD {i * 100} 库存 {i * 90}" for i in range(1, 14))
    return "\n".join(rows)


def test_activity_document_terms_are_detected_by_page_probe():
    probe = _probe(
        1,
        "商品手册\n关税合规记录\n物流记录\nSKU-A100 USD 79.90 库存 42 配送时长 3 工作日 关税税率 12%",
    )

    assert {"商品手册", "关税合规记录", "物流记录"}.issubset(probe.table_title_hits)
    assert {"商品手册", "关税合规", "物流记录"}.issubset(probe.metric_hits)
    assert "ecommerce_table_title" in classify(probe)


def test_english_activity_terms_are_case_insensitive():
    probe = _probe(
        1,
        "product manual\ncustoms compliance\nlogistics record\nsku-a100 price usd 79.90 inventory 42 delivery 3 days",
    )

    assert {"Product Manual", "Customs Compliance", "Logistics Record"}.issubset(probe.table_title_hits)
    assert {"price", "inventory", "delivery", "logistics", "customs compliance"}.issubset(probe.metric_hits)
    assert "USD" in probe.unit_hits
    assert "ecommerce_table_title" in classify(probe)


def test_feature_classification_and_selection_match_frozen_policy():
    pages = [
        _probe(1, "普通正文" * 30),
        _probe(2, _financial_page("商品价格表")),
        _probe(3, "短页"),
        _probe(4, _financial_page()),
        _probe(5, "普通正文" * 30),
        _probe(6, "普通正文" * 30),
    ]

    reasons = classify(pages[3])
    routes = select(pages, max_pages=3)

    assert "numeric_ecommerce_page" in reasons
    assert "low_text" not in reasons
    selected = {route.page_number for route in routes if route.selected}
    assert selected == {1, 2, 4}
    assert next(route for route in routes if route.page_number == 3).dropped_by_cap
    assert ROUTING_POLICY["external_labels_used"] is False
    assert len(POLICY_FINGERPRINT) == 64


def test_router_uses_l2_table_and_skips_l3_narrative(tmp_path):
    l3_calls: list[int] = []

    def l2(file_path, page_number, *, doc_id, source):
        return [
            ParsedBlock("重复叙事", {"content_type": "text"}),
            ParsedBlock(
                "| 指标 | 2024 |\n| --- | --- |\n| 收入 | 100 |",
                {"content_type": "table"},
            ),
        ]

    def l3(file_path, page_number, *, doc_id, source):
        l3_calls.append(page_number)
        return [{"table_markdown": "| 不应 | 调用 |"}]

    router = PDFParseRouter(
        l2,
        l3,
        pdf_opener=lambda path: _PDF([_financial_page("商品价格表")]),
    )
    result = router.parse(_pdf_path(tmp_path), doc_id=7, source="report.pdf")

    assert result.status == "succeeded"
    assert l3_calls == []
    assert [block.metadata["content_type"] for block in result.blocks] == ["text", "table"]
    assert not any(block.content == "重复叙事" for block in result.blocks)
    table = result.blocks[1]
    assert table.metadata["table_id"] == "doc_7:page_1:table_1"
    assert table.metadata["provenance_id"] == "doc_7:page_1:l2:table_1"
    assert all(
        isinstance(value, (str, int, float, bool))
        for block in result.blocks
        for value in block.metadata.values()
    )


def test_router_falls_back_on_placeholder_and_preserves_l1_when_l3_fails(tmp_path):
    l3_calls: list[int] = []

    def l2(file_path, page_number, *, doc_id, source):
        return [ParsedBlock(
            "[Table on page 1: no extractable cells]",
            {"content_type": "table", "table_conversion": "placeholder"},
        )]

    def l3(file_path, page_number, *, doc_id, source):
        l3_calls.append(page_number)
        raise RuntimeError("artifact unavailable")

    router = PDFParseRouter(
        l2,
        l3,
        pdf_opener=lambda path: _PDF([_financial_page("商品价格表")]),
    )
    result = router.parse(_pdf_path(tmp_path), doc_id=1)

    assert result.status == "degraded"
    assert l3_calls == [1]
    assert len(result.blocks) == 1
    assert result.blocks[0].metadata["content_type"] == "text"
    assert result.page_routes[0].degraded is True
    assert result.page_routes[0].l3_attempted is True
    assert any("l1_preserved_degraded" in warning for warning in result.warnings)


def test_router_reports_cap_drop_and_only_fails_when_all_layers_are_empty(tmp_path):
    router = PDFParseRouter(
        max_pages=1,
        pdf_opener=lambda path: _PDF(["", ""]),
    )
    result = router.parse(_pdf_path(tmp_path, b"empty-pdf"), doc_id=1)

    assert result.status == "failed"
    assert result.page_count == 2
    assert result.selected_page_count == 1
    assert result.dropped_page_count == 1
    assert any(warning.startswith("page_cap_dropped:1") for warning in result.warnings)
    assert all(route.metadata["policy_fingerprint"] == router.policy_fingerprint for route in result.page_routes)


def test_router_accepts_adapter_result_blocks_and_propagates_pdf_sha(tmp_path):
    from dataclasses import dataclass
    import hashlib

    @dataclass
    class PaddleArtifactResult:
        blocks: tuple[ParsedBlock, ...]

    received = {}

    def l2(file_path, page_number, *, doc_id, source):
        return []

    def adapter(file_path, page_number, *, doc_id, source, pdf_sha256):
        received["pdf_sha256"] = pdf_sha256
        return PaddleArtifactResult((ParsedBlock(
            "| 指标 | 值 |\n| --- | --- |\n| 收入 | 100 |",
            {"content_type": "table"},
        ),))

    content = b"pdf-sha-fixture"
    path = _pdf_path(tmp_path, content)
    router = PDFParseRouter(
        l2,
        adapter,
        pdf_opener=lambda path: _PDF([_financial_page("商品价格表")]),
    )
    result = router.parse(path, doc_id=3)
    expected_sha = hashlib.sha256(content).hexdigest()

    assert received["pdf_sha256"] == expected_sha
    assert result.status == "succeeded"
    assert [block.metadata["pdf_sha256"] for block in result.blocks] == [expected_sha, expected_sha]
    assert result.page_routes[0].metadata["pdf_sha256"] == expected_sha
    table = result.blocks[1]
    assert table.metadata["selected_layer"] == "L3"
    assert table.metadata["fallback_from"] == "L2"
    assert table.metadata["route_path"] == "L1->L2->L3"


def test_custom_policy_changes_fingerprint_and_low_text_threshold():
    default_router = PDFParseRouter()
    custom_router = PDFParseRouter(
        numeric_ratio_min=0.25,
        line_count_min=20,
        low_text_min_chars=100,
        title_neighbor_before=0,
        title_neighbor_after=1,
    )

    assert default_router.policy_fingerprint == POLICY_FINGERPRINT
    assert default_router.policy == ROUTING_POLICY
    assert default_router.policy_fingerprint != custom_router.policy_fingerprint
    assert page_features("正文" * 20, "r.pdf", 1).low_text is False
    assert page_features(
        "正文" * 20,
        "r.pdf",
        1,
        policy=custom_router.policy,
    ).low_text is True


def test_l2_disabled_l3_success_uses_direct_route(tmp_path):
    def l3(file_path, page_number, *, doc_id, source, pdf_sha256):
        return [ParsedBlock("| 指标 | 值 |\n| --- | --- |\n| 收入 | 100 |", {"content_type": "table"})]

    router = PDFParseRouter(
        artifact_adapter=l3,
        pdf_opener=lambda path: _PDF([_financial_page("商品价格表")]),
    )
    result = router.parse(_pdf_path(tmp_path), doc_id=1)
    route = result.page_routes[0]
    table = result.blocks[1]

    assert result.status == "succeeded"
    assert route.l2_status == "disabled"
    assert route.l3_status == "succeeded"
    assert route.metadata["route_path"] == "L1->L3"
    assert route.metadata["fallback_from"] == ""
    assert table.metadata["route_path"] == "L1->L3"
    assert table.metadata["fallback_from"] == ""


def test_l2_empty_l3_disabled_is_degraded(tmp_path):
    def l2(file_path, page_number, *, doc_id, source):
        return []

    router = PDFParseRouter(
        hi_res_page_parser=l2,
        pdf_opener=lambda path: _PDF([_financial_page("商品价格表")]),
    )
    result = router.parse(_pdf_path(tmp_path), doc_id=1)
    route = result.page_routes[0]

    assert result.status == "degraded"
    assert route.l2_status == "empty"
    assert route.l3_status == "disabled"
    assert route.metadata["route_path"] == "L1->L2"
    assert route.metadata["parse_status"] == "degraded"


def test_both_optional_layers_disabled_candidate_is_degraded(tmp_path):
    router = PDFParseRouter(
        pdf_opener=lambda path: _PDF([_financial_page("商品价格表")]),
    )
    result = router.parse(_pdf_path(tmp_path), doc_id=1)
    route = result.page_routes[0]

    assert result.status == "degraded"
    assert route.l2_status == route.l3_status == "disabled"
    assert route.metadata["route_path"] == "L1"
    assert route.metadata["selected_layer"] == "L1"
    assert route.metadata["parse_status"] == "degraded"


def test_custom_policy_fingerprint_is_consistent_across_result_metadata(tmp_path):
    router = PDFParseRouter(
        low_text_min_chars=100,
        pdf_opener=lambda path: _PDF(["正文" * 20]),
    )
    result = router.parse(_pdf_path(tmp_path), doc_id=1)
    block = result.blocks[0]

    assert result.policy_fingerprint == router.policy_fingerprint
    assert block.metadata["policy_fingerprint"] == router.policy_fingerprint
    assert result.page_routes[0].metadata["policy_fingerprint"] == router.policy_fingerprint
    assert router.policy_fingerprint in block.metadata["page_features"]
