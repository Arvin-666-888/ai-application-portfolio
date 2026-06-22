import json
import logging

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Conversation, Message
from app.services.document_service import get_embedding
from app.utils.retrieval import build_source_items, financial_guardrail_refusal
from app.utils.vector_store import vector_store

logger = logging.getLogger("kb_qa.rag")

RAG_SYSTEM_PROMPT = """你是一个专业的企业知识库助手。请根据以下参考资料回答用户的问题。

要求：
1. 只根据参考资料中的信息回答，不要编造内容
2. 如果参考资料中没有相关信息，请明确说"根据现有资料无法回答该问题"
3. 回答要简洁准确
4. 使用中文回答"""


def format_context(contexts: list[dict]) -> str:
    if not contexts:
        return "（无相关参考资料）"

    formatted = []
    for i, ctx in enumerate(contexts, 1):
        formatted.append(f"[来源{i}: {ctx['source']}]\n{ctx['content']}")
    return "\n\n".join(formatted)


def build_messages(
    system_prompt: str,
    history: list[Message],
    question: str,
    context: str,
    max_history_rounds: int = 5,
) -> list[dict]:
    messages = [{"role": "system", "content": system_prompt}]

    recent = history[-(max_history_rounds * 2):] if history else []
    for msg in recent:
        messages.append({"role": msg.role, "content": msg.content})

    user_content = f"参考资料：\n{context}\n\n问题：{question}"
    messages.append({"role": "user", "content": user_content})

    return messages


async def retrieve_context(query: str, kb_id: int, top_k: int = None) -> list[dict]:
    top_k = top_k or settings.TOP_K
    query_embedding = await get_embedding(query)
    candidate_multiplier = max(1, settings.RETRIEVAL_CANDIDATE_MULTIPLIER)
    return vector_store.query(
        kb_id=kb_id,
        query_embedding=query_embedding,
        top_k=top_k,
        query_text=query,
        candidate_multiplier=candidate_multiplier,
    )


async def generate_answer(messages: list[dict]) -> str:
    if not settings.API_KEY:
        return _mock_answer(messages)

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{settings.BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.API_KEY}"},
            json={
                "model": settings.MODEL,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 1000,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


async def stream_answer(messages: list[dict]):
    if not settings.API_KEY:
        mock_text = _mock_answer(messages)
        for char in mock_text:
            yield f"data: {json.dumps({'content': char}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{settings.BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.API_KEY}"},
            json={
                "model": settings.MODEL,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 1000,
                "stream": True,
            },
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        content = chunk["choices"][0]["delta"].get("content", "")
                        if content:
                            yield f"data: {json.dumps({'content': content}, ensure_ascii=False)}\n\n"
                    except json.JSONDecodeError:
                        continue
    yield "data: [DONE]\n\n"


def _mock_answer(messages: list[dict]) -> str:
    last_user_msg = ""
    for msg in reversed(messages):
        if msg["role"] == "user":
            last_user_msg = msg["content"]
            break

    if "参考资料" in last_user_msg:
        parts = last_user_msg.split("问题：")
        question = parts[-1].strip() if len(parts) > 1 else "未知问题"
        context_part = last_user_msg.split("参考资料：")[1].split("问题：")[0] if "参考资料：" in last_user_msg else ""

        if context_part and context_part.strip() != "（无相关参考资料）":
            return f"[模拟回答] 关于「{question}」，根据参考资料中的信息，这是一个模拟回答。请配置 API_KEY 以获取真实的大模型回答。"
        else:
            return "[模拟回答] 根据现有资料无法回答该问题。请配置 API_KEY 并上传文档以获取真实回答。"

    return "[模拟回答] 请配置 API_KEY 以获取真实的大模型回答。"


def create_conversation(db: Session, kb_id: int, user_id: int, title: str = "新对话") -> Conversation:
    kb_exists = db.query(Conversation).filter(
        Conversation.kb_id == kb_id,
        Conversation.user_id == user_id,
    ).first()
    if not kb_exists:
        from app.models.models import KnowledgeBase
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb or kb.user_id != user_id:
            raise ValueError("知识库不存在或无权访问")

    conv = Conversation(title=title, kb_id=kb_id, user_id=user_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def list_conversations(db: Session, user_id: int) -> list[Conversation]:
    return db.query(Conversation).filter(
        Conversation.user_id == user_id
    ).order_by(Conversation.created_at.desc()).all()


def get_conversation_messages(db: Session, conversation_id: int, user_id: int) -> list[Message]:
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv or conv.user_id != user_id:
        raise ValueError("对话不存在或无权访问")

    return db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.created_at.asc()).all()


def save_message(db: Session, conversation_id: int, role: str, content: str, sources: list[dict] = None) -> Message:
    msg = Message(
        role=role,
        content=content,
        sources=sources,
        conversation_id=conversation_id,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def build_sources(contexts: list[dict]) -> list[dict]:
    return build_source_items(contexts)


async def answer_question(question: str, kb_id: int, history: list[Message] = None) -> tuple[str, list[dict]]:
    refusal = financial_guardrail_refusal(question)
    if refusal:
        return refusal, []

    contexts = await retrieve_context(question, kb_id)
    context_str = format_context(contexts)
    messages = build_messages(
        system_prompt=RAG_SYSTEM_PROMPT,
        history=history or [],
        question=question,
        context=context_str,
        max_history_rounds=settings.MAX_HISTORY_ROUNDS,
    )
    answer = await generate_answer(messages)
    return answer, build_sources(contexts)
