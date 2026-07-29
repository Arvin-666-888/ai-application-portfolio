from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.models import Conversation, KnowledgeBase, User
from app.repositories import rag_run_repository


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory(), engine


def test_rag_run_lifecycle_and_owned_lookup(monkeypatch):
    db, engine = _db()
    try:
        user = User(username="owner", hashed_password="hash")
        other = User(username="other", hashed_password="hash")
        db.add_all([user, other])
        db.flush()
        kb = KnowledgeBase(name="kb", description="", user_id=user.id)
        db.add(kb)
        db.flush()
        conv = Conversation(title="chat", kb_id=kb.id, user_id=user.id)
        db.add(conv)
        db.commit()

        run = rag_run_repository.create_run(
            db,
            user_id=user.id,
            kb_id=kb.id,
            conversation_id=conv.id,
            question="2024 年营业收入是多少？",
            transport="sync",
            active_index_targets=[(1, "v1")],
        )
        rag_run_repository.complete_run(
            db,
            run,
            status="answered",
            verification_status="passed",
            candidate_count=2,
            citation_count=1,
            answer_fact_count=1,
            total_ms=123,
            usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )

        assert rag_run_repository.get_owned_run(
            db, trace_id=run.trace_id, user_id=user.id
        ) is run
        assert rag_run_repository.get_owned_run(
            db, trace_id=run.trace_id, user_id=other.id
        ) is None
        assert run.active_index_targets == [{"doc_id": 1, "index_version": "v1"}]
        assert run.status == "answered"
        assert run.usage_complete is True
        assert run.estimated_cost_amount is None
        assert run.cost_source == "unavailable"
    finally:
        db.close()
        engine.dispose()


def test_estimate_cost_uses_configured_decimal_rates(monkeypatch):
    monkeypatch.setattr(rag_run_repository.settings, "LLM_INPUT_COST_PER_1M", "2")
    monkeypatch.setattr(rag_run_repository.settings, "LLM_OUTPUT_COST_PER_1M", "8")
    monkeypatch.setattr(rag_run_repository.settings, "EMBEDDING_COST_PER_1M", "0.5")

    result = rag_run_repository.estimate_cost(
        {"input_tokens": 1000, "output_tokens": 100, "embedding_input_tokens": 2000}
    )

    assert result == Decimal("0.00380000")
