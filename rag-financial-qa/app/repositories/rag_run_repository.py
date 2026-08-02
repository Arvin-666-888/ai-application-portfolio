from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import RagRun


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def create_run(
    db: Session,
    *,
    user_id: int,
    kb_id: int,
    conversation_id: int,
    question: str,
    transport: str,
    active_index_targets: list[tuple[int, str]] | None = None,
    user_message_id: int | None = None,
) -> RagRun:
    run = RagRun(
        trace_id=str(uuid.uuid4()),
        user_id=user_id,
        kb_id=kb_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        transport=transport,
        answer_profile=settings.RAG_ANSWER_PROFILE,
        retrieval_profile=settings.RETRIEVAL_PROFILE,
        model=settings.MODEL,
        embedding_model=settings.EMBEDDING_MODEL,
        status="started",
        active_index_targets=[
            {"doc_id": int(doc_id), "index_version": str(version)}
            for doc_id, version in (active_index_targets or [])
        ],
        question_sha256=hashlib.sha256(question.encode("utf-8")).hexdigest(),
        prompt_template_version="rag-answer-v3",
        prompt_config_sha256=_prompt_config_sha256(),
        created_at=utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def complete_run(
    db: Session,
    run: RagRun,
    *,
    status: str,
    assistant_message_id: int | None = None,
    refusal_code: str | None = None,
    verification_status: str | None = None,
    verification_reason_codes: list[str] | None = None,
    candidate_count: int = 0,
    citation_count: int = 0,
    answer_fact_count: int = 0,
    retrieval_ms: int | None = None,
    generation_ms: int | None = None,
    verification_ms: int | None = None,
    persistence_ms: int | None = None,
    total_ms: int | None = None,
    usage: dict[str, int | None] | None = None,
    error_code: str | None = None,
) -> RagRun:
    usage = usage or {}
    run.status = status
    run.assistant_message_id = assistant_message_id
    run.refusal_code = refusal_code
    run.verification_status = verification_status
    run.verification_reason_codes = verification_reason_codes or []
    run.candidate_count = candidate_count
    run.citation_count = citation_count
    run.answer_fact_count = answer_fact_count
    run.retrieval_ms = retrieval_ms
    run.generation_ms = generation_ms
    run.verification_ms = verification_ms
    run.persistence_ms = persistence_ms
    run.total_ms = total_ms
    run.embedding_input_tokens = usage.get("embedding_input_tokens")
    run.chat_input_tokens = usage.get("input_tokens")
    run.chat_output_tokens = usage.get("output_tokens")
    run.total_tokens = usage.get("total_tokens")
    run.usage_complete = all(
        usage.get(key) is not None for key in ("input_tokens", "output_tokens", "total_tokens")
    )
    amount = estimate_cost(usage)
    run.estimated_cost_amount = str(amount) if amount is not None else None
    run.estimated_cost_currency = settings.COST_CURRENCY if amount is not None else None
    run.cost_source = "configured_rates" if amount is not None else "unavailable"
    run.error_code = error_code
    run.completed_at = utcnow()
    db.commit()
    db.refresh(run)
    return run


def get_owned_run(db: Session, *, trace_id: str, user_id: int) -> RagRun | None:
    return db.scalar(
        select(RagRun).where(RagRun.trace_id == trace_id, RagRun.user_id == user_id)
    )


def estimate_cost(usage: dict[str, int | None]) -> Decimal | None:
    rates = {
        "input_tokens": settings.LLM_INPUT_COST_PER_1M,
        "output_tokens": settings.LLM_OUTPUT_COST_PER_1M,
        "embedding_input_tokens": settings.EMBEDDING_COST_PER_1M,
    }
    if not any(rates.values()):
        return None
    total = Decimal("0")
    for key, configured_rate in rates.items():
        tokens = usage.get(key)
        if not configured_rate or tokens is None:
            continue
        try:
            total += Decimal(str(tokens)) * Decimal(configured_rate) / Decimal("1000000")
        except (InvalidOperation, TypeError) as exc:
            raise ValueError(f"invalid configured cost rate for {key}") from exc
    return total.quantize(Decimal("0.00000001"))


def _prompt_config_sha256() -> str:
    payload: dict[str, Any] = {
        "answer_profile": settings.RAG_ANSWER_PROFILE,
        "retrieval_profile": settings.RETRIEVAL_PROFILE,
        "model": settings.MODEL,
        "top_k": settings.TOP_K,
        "context_max_chars": settings.RAG_CONTEXT_MAX_CHARS,
        "context_item_max_chars": settings.RAG_CONTEXT_ITEM_MAX_CHARS,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
