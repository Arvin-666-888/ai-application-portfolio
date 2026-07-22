from dataclasses import dataclass
from time import perf_counter

from app.agents.contracts import AftersalesDecision
from app.services.policy_service import AftersalesPolicyService


@dataclass(frozen=True, slots=True)
class EvaluatePolicyToolResult:
    policy: dict
    trace: dict


class EvaluateAftersalesPolicyTool:
    name = "evaluate_aftersales_policy"

    def __init__(self, service: AftersalesPolicyService):
        self.service = service

    def execute(
        self,
        *,
        decision: AftersalesDecision,
        order: dict,
        shipment: dict | None,
        request_id: str,
        step: int = 2,
    ) -> EvaluatePolicyToolResult:
        started = perf_counter()
        evaluation = self.service.evaluate(
            decision=decision,
            order=order,
            shipment=shipment,
        )
        policy = evaluation.model_dump(mode="json")
        return EvaluatePolicyToolResult(
            policy=policy,
            trace={
                "step": step,
                "request_id": request_id,
                "tool": self.name,
                "arguments": {
                    "issue_type": decision.issue_type,
                    "requested_action": decision.requested_action,
                    "order_status": order["status"],
                    "shipment_exception": (
                        shipment.get("exception_type", "none") if shipment else "none"
                    ),
                },
                "success": True,
                "result_count": 1,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
