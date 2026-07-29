from __future__ import annotations

import asyncio
import builtins
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import document_service
from app.utils.paddle_artifact_adapter import PaddleArtifactAdapter
from app.utils.table_pdf_parser import ParsedBlock


class _Query:
    def __init__(self, document):
        self.document = document

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.document


class _DB:
    def __init__(self, document):
        self.document = document
        self.commits = 0

    def query(self, model):
        return _Query(self.document)

    def commit(self):
        self.commits += 1


def _document():
    return SimpleNamespace(
        id=7,
        filename="report.pdf",
        kb_id=3,
        status="pending",
        error_message="old error",
        chunk_count=0,
    )


def _parse_result(status: str):
    return SimpleNamespace(
        status=status,
        page_count=6,
        selected_page_count=2,
        dropped_page_count=1,
        warnings=("warning-1",),
        policy_fingerprint="f" * 64,
    )


def _chunk(text: str = "足够长的有效知识片段内容"):
    return SimpleNamespace(content=text, metadata={"source": "report.pdf"})


def test_parse_file_txt_and_markdown_never_build_pdf_router(tmp_path, monkeypatch):
    txt_path = tmp_path / "notes.txt"
    md_path = tmp_path / "notes.md"
    txt_path.write_text("纯文本内容", encoding="utf-8")
    md_path.write_text("# Markdown 内容", encoding="utf-8")

    def unexpected_router():
        raise AssertionError("TXT/MD must not build the PDF router")

    monkeypatch.setattr(document_service, "_build_pdf_router", unexpected_router)

    txt_blocks = document_service.parse_file(str(txt_path), doc_id=1)
    md_blocks = document_service.parse_file(str(md_path), doc_id=2)

    assert [block.content for block in txt_blocks] == ["纯文本内容"]
    assert [block.content for block in md_blocks] == ["# Markdown 内容"]
    assert txt_blocks[0].metadata["parser"] == "plain_text"
    assert md_blocks[0].metadata["parser"] == "plain_text"


def test_parse_file_explicit_false_keeps_fast_parser(tmp_path, monkeypatch):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf")
    calls = []
    expected = [ParsedBlock("fast result", {"content_type": "text"})]

    class FakeParser:
        def __init__(self, *, use_hi_res):
            calls.append(("init", use_hi_res))

        def parse(self, file_path, *, doc_id, source):
            calls.append(("parse", file_path, doc_id, source))
            return expected

    monkeypatch.setattr(document_service, "TablePDFParser", FakeParser)
    monkeypatch.setattr(
        document_service,
        "_build_pdf_router",
        lambda: (_ for _ in ()).throw(AssertionError("router must not be used")),
    )

    blocks = document_service.parse_file(
        str(path), doc_id=9, source="original.pdf", use_hi_res=False
    )

    assert blocks is expected
    assert calls == [
        ("init", False),
        ("parse", str(path), 9, "original.pdf"),
    ]


def test_parse_file_default_pdf_uses_router(tmp_path, monkeypatch):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf")
    expected = (ParsedBlock("routed", {"content_type": "text"}),)
    calls = []

    class FakeRouter:
        def parse(self, file_path, *, doc_id, source):
            calls.append((file_path, doc_id, source))
            return SimpleNamespace(blocks=expected)

    monkeypatch.setattr(document_service.settings, "PDF_PARSE_PROFILE", "three_layer_v1")
    monkeypatch.setattr(document_service, "_build_pdf_router", lambda: FakeRouter())
    monkeypatch.setattr(
        document_service,
        "TablePDFParser",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("default three-layer PDF must use router")
        ),
    )

    blocks = document_service.parse_file(str(path), doc_id=4)

    assert blocks == list(expected)
    assert calls == [(str(path), 4, "report.pdf")]


def test_parse_hi_res_page_maps_physical_page_and_cleans_temp_file(
    tmp_path, monkeypatch
):
    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"source")
    seen = {}

    class FakeReader:
        def __init__(self, file_path):
            seen["reader_path"] = file_path
            self.pages = ["page-one", "page-two", "page-three"]

    class FakeWriter:
        def __init__(self):
            self.pages = []

        def add_page(self, page):
            self.pages.append(page)

        def write(self, output):
            seen["written_pages"] = list(self.pages)
            output.write(b"single-page-pdf")

    fake_pypdf = types.ModuleType("pypdf")
    fake_pypdf.PdfReader = FakeReader
    fake_pypdf.PdfWriter = FakeWriter
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    expected = [ParsedBlock("table", {"physical_page_number": 2})]

    class FakeParser:
        def __init__(self, *, use_hi_res):
            assert use_hi_res is True

        def parse_page(
            self, file_path, *, doc_id, source, physical_page_number
        ):
            temp_path = Path(file_path)
            seen.update(
                temp_path=temp_path,
                temp_exists_during_parse=temp_path.is_file(),
                temp_bytes=temp_path.read_bytes(),
                doc_id=doc_id,
                source=source,
                physical_page_number=physical_page_number,
            )
            return expected

    monkeypatch.setattr(document_service, "TablePDFParser", FakeParser)

    blocks = document_service._parse_hi_res_page(
        source_pdf, 2, doc_id=11, source="annual.pdf", pdf_sha256="a" * 64
    )

    assert blocks is expected
    assert seen["reader_path"] == str(source_pdf)
    assert seen["written_pages"] == ["page-two"]
    assert seen["temp_exists_during_parse"] is True
    assert seen["temp_bytes"] == b"single-page-pdf"
    assert seen["physical_page_number"] == 2
    assert seen["doc_id"] == 11
    assert seen["source"] == "annual.pdf"
    assert not seen["temp_path"].exists()


def test_parse_hi_res_page_cleans_temp_file_when_parser_fails(tmp_path, monkeypatch):
    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"source")
    seen = {}

    class FakeReader:
        def __init__(self, file_path):
            self.pages = [object()]

    class FakeWriter:
        def add_page(self, page):
            pass

        def write(self, output):
            output.write(b"single-page-pdf")

    fake_pypdf = types.ModuleType("pypdf")
    fake_pypdf.PdfReader = FakeReader
    fake_pypdf.PdfWriter = FakeWriter
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    class FailingParser:
        def __init__(self, *, use_hi_res):
            pass

        def parse_page(self, file_path, **kwargs):
            seen["temp_path"] = Path(file_path)
            raise RuntimeError("parse failed")

    monkeypatch.setattr(document_service, "TablePDFParser", FailingParser)

    with pytest.raises(RuntimeError, match="parse failed"):
        document_service._parse_hi_res_page(
            source_pdf, 1, doc_id=1, source="source.pdf"
        )

    assert not seen["temp_path"].exists()


def test_build_pdf_router_consumes_settings_and_uses_artifact_adapter(
    monkeypatch
):
    captured = {}
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.lower().startswith("paddle"):
            raise AssertionError("building the router must not import Paddle")
        return original_import(name, *args, **kwargs)

    class FakeRouter:
        def __init__(self, hi_res_parser, artifact_adapter, **kwargs):
            captured.update(
                hi_res_parser=hi_res_parser,
                artifact_adapter=artifact_adapter,
                kwargs=kwargs,
            )

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(document_service, "PDFParseRouter", FakeRouter)
    monkeypatch.setattr(document_service.settings, "PDF_HI_RES_ENABLED", True)
    monkeypatch.setattr(document_service.settings, "PDF_PADDLE_ARTIFACT_ENABLED", True)
    monkeypatch.setattr(document_service.settings, "PDF_PADDLE_ARTIFACT_DIR", "artifacts")
    monkeypatch.setattr(
        document_service.settings,
        "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT",
        "f" * 64,
    )
    monkeypatch.setattr(document_service.settings, "PDF_HI_RES_MAX_PAGES_PER_DOCUMENT", 17)
    monkeypatch.setattr(document_service.settings, "PDF_TABLE_NUMERIC_RATIO_MIN", 0.27)
    monkeypatch.setattr(document_service.settings, "PDF_TABLE_LINE_COUNT_MIN", 23)
    monkeypatch.setattr(document_service.settings, "PDF_NATIVE_TEXT_MIN_CHARS", 41)
    monkeypatch.setattr(document_service.settings, "PDF_TABLE_TITLE_NEIGHBOR_BEFORE", 2)
    monkeypatch.setattr(document_service.settings, "PDF_TABLE_TITLE_NEIGHBOR_AFTER", 4)

    router = document_service._build_pdf_router()

    assert isinstance(router, FakeRouter)
    assert captured["hi_res_parser"] is document_service._parse_hi_res_page
    assert isinstance(captured["artifact_adapter"], PaddleArtifactAdapter)
    assert str(captured["artifact_adapter"].artifact_root) == "artifacts"
    assert captured["artifact_adapter"].expected_engine_fingerprint == "f" * 64
    assert captured["kwargs"] == {
        "max_pages": 17,
        "numeric_ratio_min": 0.27,
        "line_count_min": 23,
        "low_text_min_chars": 41,
        "title_neighbor_before": 2,
        "title_neighbor_after": 4,
    }


def test_build_pdf_router_disables_optional_layers(monkeypatch):
    captured = {}

    class FakeRouter:
        def __init__(self, hi_res_parser, artifact_adapter, **kwargs):
            captured["layers"] = (hi_res_parser, artifact_adapter)

    monkeypatch.setattr(document_service, "PDFParseRouter", FakeRouter)
    monkeypatch.setattr(document_service.settings, "PDF_HI_RES_ENABLED", False)
    monkeypatch.setattr(document_service.settings, "PDF_PADDLE_ARTIFACT_ENABLED", False)

    document_service._build_pdf_router()

    assert captured["layers"] == (None, None)


def test_process_document_uses_to_thread_overlap_and_degraded_becomes_ready(
    monkeypatch, caplog
):
    doc = _document()
    db = _DB(doc)
    parse_result = _parse_result("degraded")
    calls = {}

    async def fake_to_thread(func, *args, **kwargs):
        calls["to_thread"] = (func, args, kwargs)
        return [ParsedBlock("raw", {"content_type": "text"})], parse_result

    def fake_build(blocks, splitter, *, table_row_overlap):
        calls["build"] = (blocks, splitter, table_row_overlap)
        return [_chunk()]

    async def fake_embed(texts):
        calls["embed"] = texts
        return [[0.1, 0.2]]

    def fake_add_documents(**kwargs):
        calls["add"] = kwargs

    monkeypatch.setattr(document_service.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(document_service, "build_index_chunks", fake_build)
    monkeypatch.setattr(document_service, "_batch_embed", fake_embed)
    monkeypatch.setattr(document_service.vector_store, "add_documents", fake_add_documents)
    monkeypatch.setattr(document_service.settings, "UPLOAD_DIR", "uploads-test")
    monkeypatch.setattr(document_service.settings, "PDF_TABLE_ROW_OVERLAP", 1)

    with caplog.at_level(logging.INFO, logger="kb_qa.document"):
        asyncio.run(document_service.process_document(db, doc.id))

    func, args, kwargs = calls["to_thread"]
    assert func is document_service._parse_file_with_result
    assert args == (str(Path("uploads-test") / "7_report.pdf"),)
    assert kwargs == {"doc_id": 7, "source": "report.pdf"}
    assert calls["build"][2] == 1
    assert calls["embed"] == ["足够长的有效知识片段内容"]
    assert calls["add"]["kb_id"] == 3
    assert calls["add"]["doc_id"] == 7
    assert doc.status == "ready"
    assert doc.chunk_count == 1
    assert doc.error_message == ""
    assert db.commits == 2
    assert (
        "Document 7 PDF route status=degraded pages=6 selected=2 dropped=1 "
        "warnings=1 policy=" + "f" * 64
    ) in caplog.text


def test_process_document_failed_parse_result_fails_without_vector_write(monkeypatch):
    doc = _document()
    db = _DB(doc)

    async def fake_to_thread(func, *args, **kwargs):
        return [ParsedBlock("raw", {"content_type": "text"})], _parse_result("failed")

    monkeypatch.setattr(document_service.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        document_service,
        "build_index_chunks",
        lambda *args, **kwargs: [_chunk()],
    )
    monkeypatch.setattr(
        document_service.vector_store,
        "add_documents",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("failed parse result must not reach vector store")
        ),
    )

    asyncio.run(document_service.process_document(db, doc.id))

    assert doc.status == "failed"
    assert doc.error_message == "PDF 三层解析未生成可用内容"
    assert doc.chunk_count == 0
    assert db.commits == 2


def test_process_document_embedding_count_mismatch_fails_without_vector_write(
    monkeypatch
):
    doc = _document()
    db = _DB(doc)

    async def fake_to_thread(func, *args, **kwargs):
        return [ParsedBlock("raw", {"content_type": "text"})], None

    async def mismatched_embeddings(texts):
        return [[0.1, 0.2]]

    monkeypatch.setattr(document_service.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        document_service,
        "build_index_chunks",
        lambda *args, **kwargs: [_chunk("第一个足够长的知识片段"), _chunk("第二个足够长的知识片段")],
    )
    monkeypatch.setattr(document_service, "_batch_embed", mismatched_embeddings)
    monkeypatch.setattr(
        document_service.vector_store,
        "add_documents",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("mismatched embeddings must not be written")
        ),
    )

    asyncio.run(document_service.process_document(db, doc.id))

    assert doc.status == "failed"
    assert doc.error_message == "Embedding 数量与知识片段不一致: 1 != 2"
    assert doc.chunk_count == 0
    assert db.commits == 2


def test_process_document_bounds_persisted_error_message(monkeypatch):
    doc = _document()
    db = _DB(doc)
    long_message = "错误" * 400

    async def failing_to_thread(func, *args, **kwargs):
        raise RuntimeError(long_message)

    monkeypatch.setattr(document_service.asyncio, "to_thread", failing_to_thread)
    monkeypatch.setattr(
        document_service.vector_store,
        "add_documents",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("parse failure must not reach vector store")
        ),
    )

    asyncio.run(document_service.process_document(db, doc.id))

    assert doc.status == "failed"
    assert doc.error_message == long_message[:500]
    assert len(doc.error_message) == 500
    assert db.commits == 2
