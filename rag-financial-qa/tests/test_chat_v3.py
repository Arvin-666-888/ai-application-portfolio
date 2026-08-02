from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models.models import Conversation, Document, KnowledgeBase, Message, RagRun, User
from app.routers import chat as chat_router
from app.routers.auth import get_current_user_dependency
from app.services.rag_service import AnswerExecutionResult


def _app_and_state(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    owner = User(username="owner", hashed_password="hash")
    other = User(username="other", hashed_password="hash")
    db.add_all([owner, other])
    db.flush()
    kb = KnowledgeBase(name="finance", description="", user_id=owner.id)
    db.add(kb)
    db.flush()
    conversation = Conversation(title="chat", kb_id=kb.id, user_id=owner.id)
    db.add(conversation)
    db.flush()
    db.add(
        Document(
            filename="annual.pdf",
            file_type=".pdf",
            kb_id=kb.id,
            status="ready",
            active_index_version="v3-index",
        )
    )
    db.commit()

    app = FastAPI()
    app.include_router(chat_router.router)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    async def override_user():
        return owner

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user_dependency] = override_user
    return app, factory, owner, other, conversation, engine


def _result() -> AnswerExecutionResult:
    return AnswerExecutionResult(
        answer="营业收入为 100 亿元。[C1]",
        sources=[
            {
                "document": "annual.pdf",
                "relevance": 0.9,
                "citation_id": "C1",
                "snippet": "2024年营业收入为100亿元。",
                "index_version": "v3-index",
            }
        ],
        answer_status="unverified",
        contexts=[{"content": "2024年营业收入为100亿元。"}],
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        retrieval_ms=2,
        generation_ms=3,
    )


def test_sync_chat_persists_run_and_keeps_compatible_fields(monkeypatch):
    app, factory, owner, _other, conversation, engine = _app_and_state(monkeypatch)

    async def fake_execute(*args, **kwargs):
        assert kwargs["active_index_targets"] == [(1, "v3-index")]
        return _result()

    monkeypatch.setattr(chat_router, "execute_answer", fake_execute)
    response = TestClient(app).post(
        f"/api/chat/{conversation.id}", json={"question": "营业收入是多少？"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "营业收入为 100 亿元。[C1]"
    assert payload["sources"][0]["citation_id"] == "C1"
    assert payload["run"]["trace_id"]
    assert payload["run"]["usage"]["total_tokens"] == 15
    with factory() as db:
        run = db.scalar(select(RagRun))
        assert run is not None
        assert run.user_id == owner.id
        assert run.status == "answered"
        assert run.active_index_targets == [
            {"doc_id": 1, "index_version": "v3-index"}
        ]
        assert run.question_sha256
    engine.dispose()


def test_sse_uses_verified_final_result_event_order(monkeypatch):
    app, _factory, _owner, _other, conversation, engine = _app_and_state(monkeypatch)
    monkeypatch.setattr(chat_router.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def fake_execute(*args, **kwargs):
        return _result()

    monkeypatch.setattr(chat_router, "execute_answer", fake_execute)
    response = TestClient(app).post(
        f"/api/chat/{conversation.id}/stream", json={"question": "营业收入是多少？"}
    )

    assert response.status_code == 200
    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert [json.loads(item)["type"] for item in events[:-1]] == [
        "meta", "content", "sources", "result"
    ]
    assert events[-1] == "[DONE]"
    engine.dispose()


def test_legacy_sse_keeps_incremental_content_contract(monkeypatch):
    app, factory, _owner, _other, conversation, engine = _app_and_state(monkeypatch)
    monkeypatch.setattr(chat_router.settings, "RAG_ANSWER_PROFILE", "legacy")

    async def fake_prepare(*args, **kwargs):
        return None, [{"role": "user", "content": "q"}], [
            {"source": "annual.pdf", "content": "evidence", "distance": 0.1}
        ]

    async def fake_stream(_messages):
        yield 'data: {"content":"A"}\n\n'
        yield 'data: {"content":"B"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_router, "prepare_question", fake_prepare)
    monkeypatch.setattr(chat_router, "stream_answer", fake_stream)
    response = TestClient(app).post(
        f"/api/chat/{conversation.id}/stream", json={"question": "营业收入是多少？"}
    )

    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert events == ['{"content":"A"}', '{"content":"B"}', "[DONE]"]
    with factory() as db:
        messages = db.execute(select(Message).order_by(Message.id)).scalars().all()
        assert messages[-1].content == "AB"
        run = db.scalar(select(RagRun))
        assert run.status == "answered"
        assert run.transport == "sse"
    engine.dispose()


def test_legacy_stream_failure_before_first_chunk_returns_502(monkeypatch):
    app, factory, _owner, _other, conversation, engine = _app_and_state(monkeypatch)
    monkeypatch.setattr(chat_router.settings, "RAG_ANSWER_PROFILE", "legacy")

    async def fake_prepare(*args, **kwargs):
        return None, [{"role": "user", "content": "q"}], [
            {"source": "manual.txt", "content": "evidence", "distance": 0.1}
        ]

    async def failed_stream(_messages):
        raise httpx.ConnectError("provider unavailable")
        yield ""  # pragma: no cover

    monkeypatch.setattr(chat_router, "prepare_question", fake_prepare)
    monkeypatch.setattr(chat_router, "stream_answer", failed_stream)
    response = TestClient(app).post(
        f"/api/chat/{conversation.id}/stream", json={"question": "SKU-A100价格是多少？"}
    )

    assert response.status_code == 502
    with factory() as db:
        run = db.scalar(select(RagRun))
        assert run.status == "failed"
        assert run.error_code == "upstream_error"
    engine.dispose()


def test_legacy_prepare_failure_closes_rag_run(monkeypatch):
    app, factory, _owner, _other, conversation, engine = _app_and_state(monkeypatch)
    monkeypatch.setattr(chat_router.settings, "RAG_ANSWER_PROFILE", "legacy")

    async def failed_prepare(*args, **kwargs):
        raise httpx.ConnectError("provider unavailable")

    monkeypatch.setattr(chat_router, "prepare_question", failed_prepare)
    response = TestClient(app).post(
        f"/api/chat/{conversation.id}/stream", json={"question": "营业收入是多少？"}
    )

    assert response.status_code == 502
    with factory() as db:
        run = db.scalar(select(RagRun))
        assert run.status == "failed"
        assert run.error_code == "upstream_error"
    engine.dispose()


def test_run_lookup_is_owned(monkeypatch):
    app, factory, owner, other, conversation, engine = _app_and_state(monkeypatch)

    async def fake_execute(*args, **kwargs):
        return _result()

    monkeypatch.setattr(chat_router, "execute_answer", fake_execute)
    client = TestClient(app)
    trace_id = client.post(
        f"/api/chat/{conversation.id}", json={"question": "营业收入是多少？"}
    ).json()["run"]["trace_id"]

    response = client.get(f"/api/chat/runs/{trace_id}")
    assert response.status_code == 200
    assert response.json()["trace_id"] == trace_id

    async def override_other():
        return other

    app.dependency_overrides[get_current_user_dependency] = override_other
    assert client.get(f"/api/chat/runs/{trace_id}").status_code == 404
    with factory() as db:
        assert db.scalar(select(RagRun).where(RagRun.trace_id == trace_id)).user_id == owner.id
    engine.dispose()
