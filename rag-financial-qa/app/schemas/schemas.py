from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    username: str
    created_at: datetime

    class Config:
        from_attributes = True


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class KnowledgeBaseResponse(BaseModel):
    id: int
    name: str
    description: str
    user_id: int
    document_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class DocumentResponse(BaseModel):
    id: int
    filename: str
    file_type: str
    file_size: int
    chunk_count: int
    status: str
    error_message: str = ""
    kb_id: int
    file_sha256: str = ""
    ingestion_status: str = "pending"
    enrichment_status: str = "pending"
    parse_profile: str = ""
    parse_policy_fingerprint: str = ""
    parse_audit: Optional[dict] = None
    active_index_version: str = ""
    page_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DocumentJobResponse(BaseModel):
    id: int
    document_id: int
    job_type: str
    physical_page_number: Optional[int] = None
    status: str
    attempt_count: int
    max_attempts: int
    claimed_by: Optional[str] = None
    available_at: datetime
    lease_expires_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    artifact_sha256: str = ""
    error_code: str = ""
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: datetime

    class Config:
        from_attributes = True


class ConversationCreate(BaseModel):
    kb_id: int
    title: str = Field(default="新对话", max_length=200)


class ConversationResponse(BaseModel):
    id: int
    title: str
    kb_id: int
    user_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


class SourceInfo(BaseModel):
    document: str
    relevance: float
    citation_id: Optional[str] = None
    snippet: str = ""
    page_number: Optional[int] = None
    content_type: Optional[str] = None
    provenance_id: Optional[str] = None
    table_id: Optional[str] = None
    parser_layer: Optional[str] = None
    parse_profile: Optional[str] = None
    artifact: Optional[str] = None
    index_version: Optional[str] = None
    ecommerce_v2_score: Optional[float] = None

    class Config:
        extra = "forbid"


# MIGRATION: 财务指标事实 -> 价格、库存数量、物流时效与关税税率四类电商事实。
class StructuredEcommerceFact(BaseModel):
    fact_type: Literal[
        "price", "inventory_quantity", "delivery_duration", "customs_duty_rate"
    ]
    value_text: str
    unit: Optional[Literal["hour", "day", "business_day", "percent"]] = None
    currency: Optional[Literal["CNY", "USD", "HKD"]] = None
    sku: Optional[str] = None
    product: Optional[str] = None
    platform: Optional[str] = None
    market: Optional[str] = None
    date: Optional[str] = None
    citation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fact_contract(self):
        if self.fact_type == "price" and (not self.currency or self.unit is not None):
            raise ValueError("price requires explicit currency and forbids unit")
        if self.fact_type == "inventory_quantity" and (self.currency or self.unit):
            raise ValueError("inventory_quantity forbids currency and unit")
        if self.fact_type == "delivery_duration" and self.unit not in {
            "hour", "day", "business_day"
        }:
            raise ValueError("delivery_duration requires an explicit duration unit")
        if self.fact_type == "delivery_duration" and self.currency:
            raise ValueError("delivery_duration forbids currency")
        if self.fact_type == "customs_duty_rate" and (
            self.unit != "percent" or self.currency
        ):
            raise ValueError("customs_duty_rate requires percent and forbids currency")
        return self

    class Config:
        extra = "forbid"


class StructuredAnswer(BaseModel):
    answer_text: str
    facts: list[StructuredEcommerceFact] = Field(default_factory=list)


class VerificationResult(BaseModel):
    passed: bool
    status: Literal["passed", "failed", "not_applicable"]
    errors: list[str] = Field(default_factory=list)
    verified_citation_ids: list[str] = Field(default_factory=list)


class UsageSummary(BaseModel):
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    usage_complete: bool = False


class CostSummary(BaseModel):
    currency: Optional[str] = None
    amount: Optional[str] = None
    source: Literal["configured_rates", "unavailable"] = "unavailable"


class RunSummary(BaseModel):
    trace_id: Optional[str] = None
    model: Optional[str] = None
    answer_profile: Optional[str] = None
    retrieval_profile: Optional[str] = None
    duration_ms: Optional[int] = None
    usage: Optional[UsageSummary] = None
    cost: Optional[CostSummary] = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceInfo] = Field(default_factory=list)
    answer_status: Optional[Literal["verified", "failed", "unverified", "refused"]] = None
    structured_answer: Optional[StructuredAnswer] = None
    verification: Optional[VerificationResult] = None
    run: Optional[RunSummary] = None


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    sources: Optional[list[SourceInfo]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ApiResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: Optional[dict | list] = None
