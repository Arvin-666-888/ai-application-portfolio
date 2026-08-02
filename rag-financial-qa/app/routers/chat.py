from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import get_db
from app.models.models import Conversation, Document, KnowledgeBase, RagRun, User
from app.repositories import rag_run_repository
from app.routers.auth import get_current_user_dependency
from app.schemas.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationCreate,
    ConversationResponse,
    CostSummary,
    MessageResponse,
    RunSummary,
    SourceInfo,
    UsageSummary,
)
from app.services.rag_service import (
    AnswerExecutionResult,
    build_sources,
    create_conversation,
    execute_answer,
    get_conversation_messages,
    list_conversations,
    prepare_question,
    save_message,
    stream_answer,
)

logger = logging.getLogger("kb_qa.chat_router")
router = APIRouter(prefix="/api/chat", tags=["智能问答"])


def _active_index_targets(db: Session, kb_id: int) -> list[tuple[int, str]]:
    rows = db.query(Document.id, Document.active_index_version).filter(
        Document.kb_id == kb_id,
        Document.status == "ready",
    ).all()
    if any(not version for _, version in rows):
        raise RuntimeError("知识库存在尚未完成索引版本迁移的 ready 文档，拒绝混合版本查询")
    return sorted((int(doc_id), str(version)) for doc_id, version in rows)


def _owned_conversation(db: Session, conversation_id: int, user_id: int) -> Conversation:
    conversation = (
        db.query(Conversation)
        .join(KnowledgeBase, KnowledgeBase.id == Conversation.kb_id)
        .filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            KnowledgeBase.user_id == user_id,
        )
        .first()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conversation


def _run_summary(run: RagRun) -> RunSummary:
    return RunSummary(
        trace_id=run.trace_id,
        model=run.model,
        answer_profile=run.answer_profile,
        retrieval_profile=run.retrieval_profile,
        duration_ms=run.total_ms,
        usage=UsageSummary(
            input_tokens=run.chat_input_tokens,
            output_tokens=run.chat_output_tokens,
            total_tokens=run.total_tokens,
            usage_complete=run.usage_complete,
        ),
        cost=CostSummary(
            currency=run.estimated_cost_currency,
            amount=run.estimated_cost_amount,
            source=("configured_rates" if run.cost_source == "configured_rates" else "unavailable"),
        ),
    )


def _chat_response(result: AnswerExecutionResult, run: RagRun) -> ChatResponse:
    return ChatResponse(
        answer=result.answer,
        sources=[SourceInfo(**source) for source in result.sources],
        answer_status=result.answer_status,
        structured_answer=result.structured_answer,
        verification=result.verification,
        run=_run_summary(run),
    )


def _complete_audit_run(
    db: Session,
    run: RagRun,
    result: AnswerExecutionResult,
    *,
    assistant_message_id: int,
    total_ms: int,
    persistence_ms: int,
) -> RagRun:
    status = "refused" if result.answer_status == "refused" else "answered"
    return rag_run_repository.complete_run(
        db,
        run,
        status=status,
        assistant_message_id=assistant_message_id,
        refusal_code=result.refusal_code,
        verification_status=(result.verification.status if result.verification else None),
        verification_reason_codes=(result.verification.errors if result.verification else []),
        candidate_count=len(result.contexts),
        citation_count=len(result.sources),
        answer_fact_count=(len(result.structured_answer.facts) if result.structured_answer else 0),
        retrieval_ms=result.retrieval_ms,
        generation_ms=result.generation_ms,
        verification_ms=result.verification_ms,
        persistence_ms=persistence_ms,
        total_ms=total_ms,
        usage=result.usage,
    )


async def _execute_and_persist(
    db: Session,
    *,
    conversation: Conversation,
    user: User,
    question: str,
    transport: str,
) -> tuple[AnswerExecutionResult, RagRun]:
    started = time.perf_counter()
    targets = _active_index_targets(db, conversation.kb_id)
    user_message = save_message(db, conversation.id, "user", question)
    history = get_conversation_messages(db, conversation.id, user.id)
    history_without_last = history[:-1] if history else []
    run = rag_run_repository.create_run(
        db,
        user_id=user.id,
        kb_id=conversation.kb_id,
        conversation_id=conversation.id,
        question=question,
        transport=transport,
        active_index_targets=targets,
        user_message_id=user_message.id,
    )
    try:
        result = await execute_answer(
            question,
            conversation.kb_id,
            history_without_last,
            active_index_targets=targets,
        )
        persistence_started = time.perf_counter()
        assistant = save_message(
            db, conversation.id, "assistant", result.answer, result.sources
        )
        persistence_ms = int((time.perf_counter() - persistence_started) * 1000)
        total_ms = int((time.perf_counter() - started) * 1000)
        run = _complete_audit_run(
            db,
            run,
            result,
            assistant_message_id=assistant.id,
            total_ms=total_ms,
            persistence_ms=persistence_ms,
        )
        return result, run
    except asyncio.CancelledError:
        total_ms = int((time.perf_counter() - started) * 1000)
        try:
            rag_run_repository.complete_run(
                db,
                run,
                status="cancelled",
                total_ms=total_ms,
                error_code="request_cancelled",
            )
        except Exception:
            logger.exception("Failed to persist cancelled RagRun trace=%s", run.trace_id)
        raise
    except Exception as exc:
        total_ms = int((time.perf_counter() - started) * 1000)
        try:
            rag_run_repository.complete_run(
                db,
                run,
                status="failed",
                total_ms=total_ms,
                error_code=("upstream_error" if isinstance(exc, httpx.HTTPError) else "internal_error"),
            )
        except Exception:
            logger.exception("Failed to persist RagRun failure trace=%s", run.trace_id)
        logger.exception("Chat execution failed trace=%s", run.trace_id)
        raise HTTPException(
            status_code=502 if isinstance(exc, httpx.HTTPError) else 500,
            detail={"code": "upstream_error" if isinstance(exc, httpx.HTTPError) else "internal_error", "trace_id": run.trace_id},
        ) from exc


@router.post("/conversations", response_model=ConversationResponse)
async def new_conversation(
    req: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        return create_conversation(db, req.kb_id, current_user.id, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/conversations", response_model=list[ConversationResponse])
async def get_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    return list_conversations(db, current_user.id)


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        messages = get_conversation_messages(db, conv_id, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        MessageResponse(
            id=message.id,
            role=message.role,
            content=message.content,
            sources=(
                [SourceInfo(**source) for source in message.sources]
                if message.sources
                else None
            ),
            created_at=message.created_at,
        )
        for message in messages
    ]


@router.get("/runs/{trace_id}")
async def get_run(
    trace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    run = rag_run_repository.get_owned_run(db, trace_id=trace_id, user_id=current_user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return {
        "trace_id": run.trace_id,
        "status": run.status,
        "refusal_code": run.refusal_code,
        "verification_status": run.verification_status,
        "verification_reason_codes": run.verification_reason_codes or [],
        "candidate_count": run.candidate_count,
        "citation_count": run.citation_count,
        "answer_fact_count": run.answer_fact_count,
        "active_index_targets": run.active_index_targets or [],
        "run": _run_summary(run).model_dump(),
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


@router.post("/{conversation_id}", response_model=ChatResponse)
async def chat(
    conversation_id: int,
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    conversation = _owned_conversation(db, conversation_id, current_user.id)
    result, run = await _execute_and_persist(
        db,
        conversation=conversation,
        user=current_user,
        question=req.question,
        transport="sync",
    )
    return _chat_response(result, run)


@router.post("/{conversation_id}/stream")
async def chat_stream(
    conversation_id: int,
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    conversation = _owned_conversation(db, conversation_id, current_user.id)
    if settings.RAG_ANSWER_PROFILE == "legacy":
        targets = _active_index_targets(db, conversation.kb_id)
        user_message = save_message(db, conversation.id, "user", req.question)
        run = rag_run_repository.create_run(
            db,
            user_id=current_user.id,
            kb_id=conversation.kb_id,
            conversation_id=conversation.id,
            question=req.question,
            transport="sse",
            active_index_targets=targets,
            user_message_id=user_message.id,
        )
        stream_started = time.perf_counter()
        try:
            history = get_conversation_messages(db, conversation.id, current_user.id)
            refusal, messages, contexts = await prepare_question(
                req.question,
                conversation.kb_id,
                history[:-1] if history else [],
                active_index_targets=targets,
            )
        except asyncio.CancelledError:
            rag_run_repository.complete_run(
                db,
                run,
                status="cancelled",
                total_ms=int((time.perf_counter() - stream_started) * 1000),
                error_code="request_cancelled",
            )
            raise
        except Exception as exc:
            rag_run_repository.complete_run(
                db,
                run,
                status="failed",
                total_ms=int((time.perf_counter() - stream_started) * 1000),
                error_code="upstream_error" if isinstance(exc, httpx.HTTPError) else "internal_error",
            )
            raise HTTPException(
                status_code=502 if isinstance(exc, httpx.HTTPError) else 500,
                detail={"code": "upstream_error" if isinstance(exc, httpx.HTTPError) else "internal_error", "trace_id": run.trace_id},
            ) from exc
        sources = build_sources(contexts)
        session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        stream_iterator = None
        first_chunk = None
        if not refusal:
            try:
                stream_iterator = stream_answer(messages)
                first_chunk = await anext(stream_iterator, None)
            except Exception as exc:
                rag_run_repository.complete_run(
                    db,
                    run,
                    status="failed",
                    total_ms=int((time.perf_counter() - stream_started) * 1000),
                    error_code="upstream_error" if isinstance(exc, httpx.HTTPError) else "internal_error",
                )
                raise HTTPException(
                    status_code=502 if isinstance(exc, httpx.HTTPError) else 500,
                    detail={
                        "code": "upstream_error" if isinstance(exc, httpx.HTTPError) else "internal_error",
                        "trace_id": run.trace_id,
                    },
                ) from exc

        async def legacy_stream():
            full_answer = ""
            try:
                if refusal:
                    full_answer = refusal
                    yield f"data: {json.dumps({'content': refusal}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                else:
                    if first_chunk is not None:
                        yield first_chunk
                        if first_chunk.startswith("data: ") and first_chunk.strip() != "data: [DONE]":
                            try:
                                full_answer += json.loads(first_chunk[6:]).get("content", "")
                            except json.JSONDecodeError:
                                pass
                    async for chunk in stream_iterator:
                        yield chunk
                        if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                            try:
                                full_answer += json.loads(chunk[6:]).get("content", "")
                            except json.JSONDecodeError:
                                continue
            except asyncio.CancelledError:
                session = session_factory()
                try:
                    persisted = session.query(RagRun).filter(RagRun.trace_id == run.trace_id).first()
                    if persisted is not None:
                        rag_run_repository.complete_run(
                            session,
                            persisted,
                            status="cancelled",
                            total_ms=int((time.perf_counter() - stream_started) * 1000),
                            error_code="request_cancelled",
                        )
                finally:
                    session.close()
                raise
            except Exception as exc:
                session = session_factory()
                try:
                    persisted = session.query(RagRun).filter(RagRun.trace_id == run.trace_id).first()
                    if persisted is not None:
                        rag_run_repository.complete_run(
                            session,
                            persisted,
                            status="failed",
                            total_ms=int((time.perf_counter() - stream_started) * 1000),
                            error_code="upstream_error" if isinstance(exc, httpx.HTTPError) else "internal_error",
                        )
                finally:
                    session.close()
                logger.exception("Legacy SSE failed trace=%s", run.trace_id)
                raise
            session = session_factory()
            try:
                assistant = save_message(
                    session, conversation.id, "assistant", full_answer, sources
                )
                persisted_run = session.query(RagRun).filter(
                    RagRun.trace_id == run.trace_id
                ).first()
                if persisted_run is not None:
                    rag_run_repository.complete_run(
                        session,
                        persisted_run,
                        status="refused" if refusal else "answered",
                        assistant_message_id=assistant.id,
                        refusal_code="policy_refusal" if refusal else None,
                        verification_status="not_applicable",
                        candidate_count=len(contexts),
                        citation_count=len(sources),
                        total_ms=int((time.perf_counter() - stream_started) * 1000),
                    )
            finally:
                session.close()

        return StreamingResponse(legacy_stream(), media_type="text/event-stream")

    result, run = await _execute_and_persist(
        db,
        conversation=conversation,
        user=current_user,
        question=req.question,
        transport="sse",
    )
    response = _chat_response(result, run)

    async def verified_stream():
        yield f"data: {json.dumps({'type': 'meta', 'trace_id': run.trace_id}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'content', 'content': result.answer}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'sources', 'sources': result.sources}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'result', **response.model_dump(mode='json')}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(verified_stream(), media_type="text/event-stream")
