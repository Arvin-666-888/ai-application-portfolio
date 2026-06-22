from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


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
    created_at: datetime

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
    snippet: str = ""


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceInfo] = []


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
