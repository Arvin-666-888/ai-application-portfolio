from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, JSON, Index, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    knowledge_bases: Mapped[list["KnowledgeBase"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    rag_runs: Mapped[list["RagRun"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="")
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship(back_populates="knowledge_bases")
    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    error_message: Mapped[str] = mapped_column(String(500), default="")
    kb_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    ingestion_status: Mapped[str] = mapped_column(String(20), default="pending")
    enrichment_status: Mapped[str] = mapped_column(String(20), default="pending")
    parse_profile: Mapped[str] = mapped_column(String(50), default="")
    parse_policy_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    parse_audit: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active_index_version: Mapped[str] = mapped_column(String(100), default="")
    storage_path: Mapped[str] = mapped_column(String(1000), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    jobs: Mapped[list["DocumentJob"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentJob(Base):
    __tablename__ = "document_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'stale', 'cancelled')",
            name="ck_document_jobs_status",
        ),
        Index("ix_document_jobs_claim", "status", "available_at", "priority", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    physical_page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pdf_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    engine_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    profile_version: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    artifact_locator: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="jobs")

    @property
    def error_code(self) -> str:
        if not self.last_error:
            return ""
        if "lease" in self.last_error.lower():
            return "worker_lease_error"
        return "job_failed"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    kb_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship(back_populates="conversations")
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")
    rag_runs: Mapped[list["RagRun"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[dict] = mapped_column(JSON, default=None, nullable=True)
    conversation_id: Mapped[int] = mapped_column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class RagRun(Base):
    __tablename__ = "rag_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'answered', 'refused', 'failed', 'cancelled')",
            name="ck_rag_runs_status",
        ),
        Index("ix_rag_runs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kb_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assistant_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transport: Mapped[str] = mapped_column(String(20), default="sync", nullable=False)
    answer_profile: Mapped[str] = mapped_column(String(30), default="legacy", nullable=False)
    retrieval_profile: Mapped[str] = mapped_column(String(30), default="legacy", nullable=False)
    model: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="started", nullable=False, index=True)
    refusal_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    verification_reason_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    active_index_targets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    citation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    answer_fact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retrieval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    persistence_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_complete: Mapped[bool] = mapped_column(default=False, nullable=False)
    estimated_cost_amount: Mapped[str | None] = mapped_column(String(50), nullable=True)
    estimated_cost_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cost_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    question_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    prompt_config_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="rag_runs")
    conversation: Mapped["Conversation"] = relationship(back_populates="rag_runs")
