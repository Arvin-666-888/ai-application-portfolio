from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

SHOP_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$"


class UserRegister(BaseModel):
    shop_id: str = Field(..., min_length=2, max_length=64, pattern=SHOP_ID_PATTERN)
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)


class UserLogin(BaseModel):
    shop_id: str = Field(..., min_length=2, max_length=64, pattern=SHOP_ID_PATTERN)
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shop_id: str
    username: str
    created_at: datetime


class DataSourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    db_type: str = Field(..., pattern=r"^sqlite$")
    connection_string: str = Field(..., min_length=1, max_length=500)


class DataSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    db_type: str
    user_id: int
    shop_id: str
    created_at: datetime


class SchemaResponse(BaseModel):
    tables: list[dict]


class AnalysisRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    ds_id: int


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    answer: str
    sql_query: str
    chart_path: Optional[str] = None
    data: list[dict] = []
    tool_trace: list[dict] = []
    rag_sources: list[dict] = []
    created_at: datetime


class AnalysisRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    answer: str
    sql_query: str
    chart_path: Optional[str] = None
    ds_id: int
    user_id: int
    shop_id: str
    tool_count: int = 0
    created_at: datetime
