import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User
from app.routers.auth import get_current_user_dependency
from app.schemas.schemas import (
    ChatRequest, ChatResponse, ConversationCreate,
    ConversationResponse, MessageResponse, SourceInfo,
)
from app.services.rag_service import (
    retrieve_context, generate_answer, stream_answer,
    format_context, build_messages, build_sources,
    create_conversation, list_conversations,
    get_conversation_messages, save_message,
    financial_guardrail_refusal,
    RAG_SYSTEM_PROMPT,
)

logger = logging.getLogger("kb_qa.chat_router")

router = APIRouter(prefix="/api/chat", tags=["智能问答"])


@router.post("/conversations", response_model=ConversationResponse)
async def new_conversation(
    req: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        conv = create_conversation(db, req.kb_id, current_user.id, req.title)
        return conv
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/conversations", response_model=list[ConversationResponse])
async def get_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    convs = list_conversations(db, current_user.id)
    return convs


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        messages = get_conversation_messages(db, conv_id, current_user.id)
        result = []
        for msg in messages:
            sources = None
            if msg.sources:
                sources = [SourceInfo(**s) for s in msg.sources]
            result.append(MessageResponse(
                id=msg.id, role=msg.role, content=msg.content,
                sources=sources, created_at=msg.created_at,
            ))
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{conversation_id}")
async def chat(
    conversation_id: int,
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    from app.models.models import Conversation

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")

    try:
        save_message(db, conversation_id, "user", req.question)

        refusal = financial_guardrail_refusal(req.question)
        if refusal:
            save_message(db, conversation_id, "assistant", refusal, [])
            return ChatResponse(answer=refusal, sources=[])

        history = get_conversation_messages(db, conversation_id, current_user.id)
        history_without_last = history[:-1] if history else []

        contexts = await retrieve_context(req.question, conv.kb_id)
        context_str = format_context(contexts)

        messages = build_messages(
            system_prompt=RAG_SYSTEM_PROMPT,
            history=history_without_last,
            question=req.question,
            context=context_str,
            max_history_rounds=settings.MAX_HISTORY_ROUNDS,
        )

        answer = await generate_answer(messages)

        sources = build_sources(contexts)
        save_message(db, conversation_id, "assistant", answer, sources)

        return ChatResponse(
            answer=answer,
            sources=[SourceInfo(**s) for s in sources],
        )
    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=f"问答失败: {str(e)}")


@router.post("/{conversation_id}/stream")
async def chat_stream(
    conversation_id: int,
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    from app.models.models import Conversation

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")

    try:
        save_message(db, conversation_id, "user", req.question)

        refusal = financial_guardrail_refusal(req.question)
        if refusal:
            save_message(db, conversation_id, "assistant", refusal, [])
            async def refusal_stream():
                import json
                yield f"data: {json.dumps({'content': refusal}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(refusal_stream(), media_type="text/event-stream")

        history = get_conversation_messages(db, conversation_id, current_user.id)
        history_without_last = history[:-1] if history else []

        contexts = await retrieve_context(req.question, conv.kb_id)
        context_str = format_context(contexts)

        messages = build_messages(
            system_prompt=RAG_SYSTEM_PROMPT,
            history=history_without_last,
            question=req.question,
            context=context_str,
            max_history_rounds=settings.MAX_HISTORY_ROUNDS,
        )

        sources = build_sources(contexts)

        return StreamingResponse(
            _stream_with_save(conversation_id, messages, sources, db),
            media_type="text/event-stream",
        )
    except Exception as e:
        logger.error(f"Stream chat failed: {e}")
        raise HTTPException(status_code=500, detail=f"问答失败: {str(e)}")

async def _stream_with_save(conversation_id: int, messages: list[dict], sources: list[dict], db: Session):
    full_answer = ""
    async for chunk in stream_answer(messages):
        yield chunk
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            import json
            try:
                data = json.loads(chunk[6:])
                full_answer += data.get("content", "")
            except json.JSONDecodeError:
                pass

    save_message(db, conversation_id, "assistant", full_answer, sources)
