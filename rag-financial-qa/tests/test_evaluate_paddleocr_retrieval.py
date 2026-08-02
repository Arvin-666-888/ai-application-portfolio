from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "05_evaluate_paddleocr_retrieval.py"
)
SPEC = importlib.util.spec_from_file_location("paddle_retrieval_eval", SCRIPT)
assert SPEC and SPEC.loader
evaluation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluation
SPEC.loader.exec_module(evaluation)


def _chunk(content: str, source="a.pdf", doc_id=1, kind="text"):
    return evaluation.IndexChunk(
        content,
        {
            "source": source,
            "doc_id": doc_id,
            "page_number": 1,
            "content_type": kind,
            "parser": "pdfplumber_page_text" if kind == "text" else "table",
        },
    )


def _candidate_payload(*, runtime_seconds=1.0, source="report.pdf", timestamp=None):
    candidate = {"candidate_id": "candidate-1", "source": source}
    case = {
        "case_id": "case_00",
        "question": "营业收入是多少？",
        "baseline": {"fusion": [candidate]},
        "paddle": {"fusion": [candidate]},
    }
    payload = {
        "schema_version": evaluation.CANDIDATE_SCHEMA,
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "inputs": {
            "questions_sha256": "a" * 64,
            "paddle_chunks_sha256": "b" * 64,
            "baseline_corpus_sha256": "c" * 64,
            "paddle_corpus_sha256": "d" * 64,
            "routed_corpus_sha256": None,
            "config_sha256": "e" * 64,
            "candidate_cache_identity": None,
        },
        "configuration": {"retrieval_profile": "legacy"},
        "embedding_cache": {"final_hits": 1},
        "cases": [case],
        "runtime_seconds": runtime_seconds,
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp
    payload["inputs"]["candidate_cache_identity"] = evaluation.candidate_cache_identity(
        payload
    )
    payload["ranking_sha256"] = evaluation.canonical_sha256(
        evaluation.candidate_ranking_identity(payload["cases"])
    )
    return evaluation.attach_candidate_identity(payload)


def test_candidate_canonical_identity_ignores_runtime_timestamp_and_absolute_paths():
    first = _candidate_payload(
        runtime_seconds=1.25,
        source="C:/private/run/report.pdf",
        timestamp="2026-07-28T10:00:00+08:00",
    )
    second = _candidate_payload(
        runtime_seconds=99.0,
        source="D:/different/machine/report.pdf",
        timestamp="2027-01-01T00:00:00Z",
    )

    assert first["candidate_canonical_sha256"] == second["candidate_canonical_sha256"]
    assert first["ranking_sha256"] == second["ranking_sha256"]


def test_output_defaults_to_refuse_overwrite_and_canonical_force_is_blocked(tmp_path):
    existing = tmp_path / "candidate.json"
    existing.write_text('{"frozen": true}', encoding="utf-8")

    with pytest.raises(evaluation.RetrievalInputError, match="--force"):
        evaluation.ensure_output_writable(
            existing,
            force=False,
            canonical_paths=(tmp_path / "canonical.json",),
        )
    evaluation.ensure_output_writable(
        existing,
        force=True,
        canonical_paths=(tmp_path / "canonical.json",),
    )
    output = tmp_path / "written.json"
    file_sha = evaluation.write_output_artifact(
        output,
        {"status": "test"},
        force=True,
        canonical_paths=(output,),
    )
    assert file_sha == evaluation.file_sha256(output)

    with pytest.raises(evaluation.RetrievalInputError, match="canonical输出"):
        evaluation.ensure_output_writable(
            existing,
            force=True,
            canonical_paths=(existing,),
        )


def test_ranking_config_requires_recall_at_five_contract():
    evaluation.validate_ranking_config(5, 20, 0.15, 20)
    with pytest.raises(evaluation.RetrievalInputError, match="top_k=5"):
        evaluation.validate_ranking_config(3, 20, 0.15, 20)
    with pytest.raises(evaluation.RetrievalInputError, match="candidate_k"):
        evaluation.validate_ranking_config(5, 4, 0.15, 20)


def test_fair_corpora_reuses_identical_old_prefix():
    old = {"a.pdf": [_chunk("old-1"), _chunk("old-2")]}
    table = [_chunk("table", kind="table")]

    corpora = evaluation.build_fair_corpora(old, table, {"a.pdf": 1})

    assert corpora["baseline_chunk_count"] == 2
    assert corpora["paddle_chunk_count"] == 3
    assert corpora["baseline_old_corpus_sha256"] == corpora["paddle_old_prefix_sha256"]
    assert [chunk.content for chunk in corpora["augmented_by_source"]["a.pdf"]] == [
        "old-1",
        "old-2",
        "table",
    ]


def test_evaluation_records_include_all_paddle_tables(monkeypatch):
    monkeypatch.setattr(evaluation, "EXPECTED_OLD_CHUNKS", 2)
    monkeypatch.setattr(evaluation, "EXPECTED_PADDLE_CHUNKS", 1)
    old = {"a.pdf": [_chunk("old-1"), _chunk("old-2")]}
    corpora = evaluation.build_fair_corpora(
        old,
        [_chunk("table", kind="table")],
        {"a.pdf": 1},
    )

    old_records, table_records = evaluation.evaluation_records(corpora)

    assert [chunk.content for chunk in old_records] == ["old-1", "old-2"]
    assert [chunk.content for chunk in table_records] == ["table"]


def test_evaluation_records_reject_table_count_mismatch(monkeypatch):
    monkeypatch.setattr(evaluation, "EXPECTED_OLD_CHUNKS", 1)
    monkeypatch.setattr(evaluation, "EXPECTED_PADDLE_CHUNKS", 2)
    corpora = evaluation.build_fair_corpora(
        {"a.pdf": [_chunk("old")]},
        [_chunk("table", kind="table")],
        {"a.pdf": 1},
    )

    with pytest.raises(evaluation.RetrievalInputError, match="表格records"):
        evaluation.evaluation_records(corpora)


def test_embedding_namespace_changes_with_model_and_base_url(monkeypatch):
    first = evaluation.embedding_namespace_fingerprint({
        "contract": "openai-compatible-embeddings-v1",
        "base_url": "https://a.example/v1",
        "model": "model-a",
    })
    changed_model = evaluation.embedding_namespace_fingerprint({
        "contract": "openai-compatible-embeddings-v1",
        "base_url": "https://a.example/v1",
        "model": "model-b",
    })
    changed_url = evaluation.embedding_namespace_fingerprint({
        "contract": "openai-compatible-embeddings-v1",
        "base_url": "https://b.example/v1",
        "model": "model-a",
    })

    assert first != changed_model
    assert first != changed_url


def test_cache_rejects_bool_nan_infinity_and_wrong_dimension():
    assert evaluation.validate_embedding_vector([True, 1.0]) is None
    assert evaluation.validate_embedding_vector([math.nan]) is None
    assert evaluation.validate_embedding_vector([math.inf]) is None
    assert evaluation.validate_embedding_vector([1.0, 2.0], expected_dimension=3) is None
    assert evaluation.validate_embedding_vector([1, 2.5]) == [1.0, 2.5]


def test_cached_embedding_round_trip_and_atomic_write(tmp_path):
    namespace = "n" * 64
    text = "中文表格"
    path = evaluation.cache_item_path(tmp_path, namespace, evaluation.text_sha256(text))

    evaluation.write_cached_embedding(path, namespace, text, [1.0, 2.0])

    assert evaluation.load_cached_embedding(path, namespace, text) == [1.0, 2.0]
    assert not path.with_suffix(".json.tmp").exists()
    assert evaluation.load_cached_embedding(path, "wrong", text) is None


def test_cached_embeddings_reuse_duplicate_text_and_resume(tmp_path, monkeypatch):
    calls = []

    async def fake_embed(texts, batch_size=20):
        calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]

    import app.services.document_service as service

    monkeypatch.setattr(service, "_batch_embed", fake_embed)
    identity = {
        "contract": "openai-compatible-embeddings-v1",
        "base_url": "https://example/v1",
        "model": "model",
    }

    first, stats = asyncio.run(
        evaluation.get_embeddings_cached(
            ["same", "same", "other"],
            tmp_path,
            identity,
            20,
        )
    )
    second, resumed = asyncio.run(
        evaluation.get_embeddings_cached(
            ["same", "other"],
            tmp_path,
            identity,
            20,
        )
    )

    assert len(calls) == 1
    assert calls[0] == ["same", "other"]
    assert first[0] == first[1]
    assert stats["api_embedded"] == 2
    assert resumed["initial_hits"] == 2
    assert second == [first[0], first[2]]


def test_real_run_blocks_without_api_key_before_embedding(monkeypatch, tmp_path):
    args = argparse.Namespace()
    inputs = {}
    monkeypatch.setattr(evaluation.settings, "API_KEY", "")

    with pytest.raises(evaluation.RetrievalInputError, match="API_KEY"):
        asyncio.run(evaluation.run_evaluation(args, inputs))


def test_query_only_loader_rejects_ground_truth_fields(tmp_path):
    path = tmp_path / "questions.jsonl"
    path.write_text(
        json.dumps({
            "case_id": "case_00",
            "question": "营业收入是多少？",
            "expected_value": "100",
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(evaluation.RetrievalInputError, match="标签字段"):
        evaluation.load_query_only_cases(path)


def test_cache_only_complete_never_imports_embedding_service(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "EXPECTED_CASES", 1)
    identity = {
        "contract": "openai-compatible-embeddings-v1",
        "base_url": "https://example/v1",
        "model": "model",
    }
    namespace = evaluation.embedding_namespace_fingerprint(identity)
    manifest = tmp_path / namespace / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "schema_version": evaluation.CACHE_MANIFEST_SCHEMA,
        "namespace_fingerprint": namespace,
        "identity": identity,
        "embedding_dimension": 2,
    }), encoding="utf-8")
    evaluation.write_cached_embedding(
        evaluation.cache_item_path(tmp_path, namespace, evaluation.text_sha256("q")),
        namespace,
        "q",
        [1.0, 2.0],
    )

    vectors, stats = evaluation.get_embeddings_cache_only(["q"], tmp_path, identity)

    assert vectors == [[1.0, 2.0]]
    assert stats["api_embedded"] == 0
    assert stats["cache_only"] is True


def test_cache_only_blocks_on_missing_item(tmp_path):
    identity = {
        "contract": "openai-compatible-embeddings-v1",
        "base_url": "https://example/v1",
        "model": "model",
    }
    namespace = evaluation.embedding_namespace_fingerprint(identity)
    manifest = tmp_path / namespace / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "schema_version": evaluation.CACHE_MANIFEST_SCHEMA,
        "namespace_fingerprint": namespace,
        "identity": identity,
        "embedding_dimension": 2,
    }), encoding="utf-8")

    with pytest.raises(evaluation.RetrievalInputError, match="missing=1"):
        evaluation.get_embeddings_cache_only(["missing"], tmp_path, identity)


def _write_routed_fixture(path, baseline_content="old", *, tamper_l1=False, old_shape=False):
    baseline = _chunk(baseline_content)
    baseline.metadata.update({"pdf_sha256": "a" * 64})
    l1_content = baseline_content + ("-tampered" if tamper_l1 else "")
    l1_metadata = {
        **baseline.metadata,
        "chunk_index": 0,
        "parser_layer": "L1",
        "selected_layer": "L1",
        "route_path": "L1",
    }
    l3_metadata = {
        "source": "a.pdf",
        "doc_id": 1,
        "pdf_sha256": "a" * 64,
        "page_number": 1,
        "content_type": "table",
        "parser": "paddleocr-table-page-v1",
        "chunk_index": 1,
        "parser_layer": "L3",
    }
    l1 = evaluation.IndexChunk(l1_content, l1_metadata)
    l3 = evaluation.IndexChunk("table", l3_metadata)
    payload = {
        "schema_version": evaluation.ROUTED_CORPUS_SCHEMA,
        "builder_version": "legacy" if old_shape else "router-v1-routed-corpus-builder-v2",
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "routing": {
            "policy_version": "financial-pdf-routing-v1",
            "policy_fingerprint": "f" * 64,
        },
        "counts": {
            "chunk_count": 9488 if old_shape else 2,
            "l1_chunk_count": 8321 if old_shape else 1,
            "l3_chunk_count": 1167 if old_shape else 1,
        },
        "l1_corpus_sha256": evaluation.canonical_sha256(evaluation._l1_identity([l1])),
        "l3_corpus_sha256": evaluation.corpus_fingerprint([l3]),
        "chunks": [
            {"content": l1.content, "metadata": l1.metadata},
            {"content": l3.content, "metadata": l3.metadata},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return baseline


def test_routed_corpus_changes_artifact_configuration_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "EXPECTED_OLD_CHUNKS", 1)
    monkeypatch.setattr(evaluation, "EXPECTED_PADDLE_CHUNKS", 1)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_baseline = _write_routed_fixture(first_path, "first")
    second_baseline = _write_routed_fixture(second_path, "second")

    _, first = evaluation.load_routed_corpus(
        first_path, {"a.pdf": 1}, {"a.pdf": "a" * 64}, {"a.pdf": [first_baseline]}
    )
    _, second = evaluation.load_routed_corpus(
        second_path, {"a.pdf": 1}, {"a.pdf": "a" * 64}, {"a.pdf": [second_baseline]}
    )

    assert first["corpus_sha256"] != second["corpus_sha256"]
    assert first["configuration_fingerprint"] != second["configuration_fingerprint"]


def test_routed_rejects_legacy_9488_and_tampered_l1(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "EXPECTED_OLD_CHUNKS", 1)
    monkeypatch.setattr(evaluation, "EXPECTED_PADDLE_CHUNKS", 1)
    legacy = tmp_path / "legacy.json"
    baseline = _write_routed_fixture(legacy, old_shape=True)
    with pytest.raises(evaluation.RetrievalInputError, match="builder_version"):
        evaluation.load_routed_corpus(
            legacy, {"a.pdf": 1}, {"a.pdf": "a" * 64}, {"a.pdf": [baseline]}
        )

    tampered = tmp_path / "tampered.json"
    baseline = _write_routed_fixture(tampered, tamper_l1=True)
    with pytest.raises(evaluation.RetrievalInputError, match="L1"):
        evaluation.load_routed_corpus(
            tampered, {"a.pdf": 1}, {"a.pdf": "a" * 64}, {"a.pdf": [baseline]}
        )


def test_legacy_path_keeps_existing_fair_corpus_behavior():
    old = {"a.pdf": [_chunk("old")]}
    corpora = evaluation.build_fair_corpora(
        old, [_chunk("table", kind="table")], {"a.pdf": 1}
    )

    assert "routed_corpus_enabled" not in corpora
    assert [item.content for item in corpora["augmented_by_source"]["a.pdf"]] == [
        "old", "table"
    ]


def test_validate_report_is_offline_and_reports_fair_counts(tmp_path, monkeypatch):
    args = argparse.Namespace(
        embedding_cache_dir=tmp_path,
        top_k=5,
        candidate_k=20,
        numeric_weight=0.15,
    )
    old = {"a.pdf": [_chunk("old")]}
    corpora = evaluation.build_fair_corpora(
        old,
        [_chunk("table", kind="table")],
        {"a.pdf": 1},
    )
    inputs = {
        "cases": [{"question": "q"}],
        "corpora": corpora,
        "paddle_info": {"table_count": 1},
    }

    report = evaluation.build_validate_report(args, inputs)

    assert report["status"] == "VALIDATED"
    assert report["api_called"] is False
    assert report["output_written"] is False
    assert report["arms"]["old_prefixes_identical"] is True
    assert report["arms"]["baseline_chunks"] == 1
    assert report["arms"]["paddle_chunks"] == 2
