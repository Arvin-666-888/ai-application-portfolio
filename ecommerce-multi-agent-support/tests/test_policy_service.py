from app.agents.contracts import AftersalesDecision
from app.services.policy_service import AftersalesPolicyService


def decision(issue_type: str, requested_action: str) -> AftersalesDecision:
    return AftersalesDecision(
        issue_type=issue_type,
        requested_action=requested_action,
        summary="test",
        source="rule_fallback",
    )


def test_damaged_refund_requires_evidence_and_approval():
    result = AftersalesPolicyService().evaluate(
        decision=decision("damaged", "refund"),
        order={"status": "shipped"},
        shipment={"exception_type": "damaged"},
    )
    assert result.proposed_action == "refund_review"
    assert result.requires_approval is True
    assert result.eligible_for_review is True
    assert result.required_evidence
    assert "已标记 damaged" in result.rationale


def test_unconfirmed_damage_is_not_presented_as_verified():
    result = AftersalesPolicyService().evaluate(
        decision=decision("damaged", "replacement"),
        order={"status": "paid"},
        shipment={"exception_type": "none"},
    )
    assert result.proposed_action == "replacement_review"
    assert "未确认 damaged" in result.rationale


def test_cancellation_is_not_eligible_after_delivery():
    result = AftersalesPolicyService().evaluate(
        decision=decision("cancel_request", "cancel"),
        order={"status": "delivered"},
        shipment={"exception_type": "none"},
    )
    assert result.eligible_for_review is False
    assert result.proposed_action == "manual_resolution_review"
    assert result.requires_approval is True


def test_address_change_is_approval_only_before_shipping():
    result = AftersalesPolicyService().evaluate(
        decision=decision("address_change", "change_address"),
        order={"status": "processing"},
        shipment={"exception_type": "none"},
    )
    assert result.proposed_action == "address_change_review"
    assert result.requires_approval is True
    assert result.eligible_for_review is True
    assert "不保存或执行地址变更" in "；".join(result.next_steps)
