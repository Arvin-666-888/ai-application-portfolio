from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


RouteName = Literal["catalog", "order", "aftersales", "unsupported"]
RouteSource = Literal["llm", "rule_fallback"]


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: RouteName
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=300)
    source: RouteSource


CatalogCategory = Literal[
    "charger",
    "power_bank",
    "cable",
    "hub",
    "wireless_charger",
    "accessory",
    "audio",
    "adapter",
]


class CatalogSearchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str | None = Field(default=None, max_length=100)
    category: CatalogCategory | None = None
    max_price: Decimal | None = Field(default=None, gt=0)
    power_w: int | None = Field(default=None, gt=0, le=500)
    in_stock_only: bool = True
    limit: int = Field(default=5, ge=1, le=10)
    source: RouteSource


AftersalesIssueType = Literal[
    "damaged",
    "wrong_item",
    "lost",
    "delayed",
    "cancel_request",
    "return_request",
    "warranty",
    "other",
]
RequestedAction = Literal[
    "refund",
    "replacement",
    "compensation",
    "cancel",
    "return",
    "warranty_service",
    "investigate",
]


class AftersalesDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_type: AftersalesIssueType
    requested_action: RequestedAction
    summary: str = Field(min_length=1, max_length=300)
    source: RouteSource


class PolicyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_code: str = Field(min_length=1, max_length=80)
    issue_type: AftersalesIssueType
    proposed_action: str = Field(min_length=1, max_length=80)
    eligible_for_review: bool
    requires_approval: bool
    rationale: str = Field(min_length=1, max_length=500)
    required_evidence: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
