from app.agents.contracts import AftersalesDecision, PolicyEvaluation


SENSITIVE_ACTIONS = {
    "refund_review",
    "replacement_review",
    "compensation_review",
    "cancellation_review",
    "return_review",
    "warranty_review",
    "manual_resolution_review",
}


class AftersalesPolicyService:
    """V1 deterministic policy rules; V2 replaces policy text lookup with RAG."""

    def evaluate(
        self,
        *,
        decision: AftersalesDecision,
        order: dict,
        shipment: dict | None,
    ) -> PolicyEvaluation:
        issue = decision.issue_type
        requested = decision.requested_action
        order_status = order["status"]
        shipment_exception = shipment.get("exception_type", "none") if shipment else "none"

        if requested == "refund":
            action = "refund_review"
        elif requested == "replacement":
            action = "replacement_review"
        elif requested == "compensation":
            action = "compensation_review"
        elif requested == "cancel":
            action = "cancellation_review"
        elif requested == "return":
            action = "return_review"
        elif requested == "warranty_service":
            action = "warranty_review"
        elif issue in {"lost", "delayed"}:
            action = "carrier_investigation"
        else:
            action = "manual_resolution_review"

        required_evidence: list[str] = []
        next_steps = ["核对订单、物流和用户提交的售后信息"]
        eligible = order_status not in {"cancelled"}
        rationale_parts = [f"订单状态为 {order_status}"]

        if issue == "damaged":
            required_evidence = ["商品或外包装破损照片", "快递面单照片"]
            rationale_parts.append("破损类申请需提交照片证据")
            if shipment_exception == "damaged":
                rationale_parts.append("物流记录已标记 damaged 异常")
            else:
                rationale_parts.append("物流记录未确认 damaged，当前仅记录用户陈述")
        elif issue == "wrong_item":
            required_evidence = ["收到商品的 SKU 或标签照片", "外包装面单照片"]
            rationale_parts.append("错发类申请需核对实物 SKU 与订单明细")
        elif issue == "lost":
            rationale_parts.append(
                "物流记录已标记 lost" if shipment_exception == "lost" else "物流记录未确认 lost，需承运商调查"
            )
            next_steps.append("向承运商发起丢件调查")
        elif issue == "delayed":
            rationale_parts.append(
                "物流记录已标记 delayed" if shipment_exception == "delayed" else "物流记录未确认 delayed，需核对最新轨迹"
            )
            next_steps.append("核对预计送达时间并联系承运商")
        elif issue == "cancel_request":
            eligible = order_status in {"paid", "processing"}
            rationale_parts.append("仅未发货订单可进入取消审核")
        elif issue == "return_request":
            required_evidence = ["商品当前状态说明"]
            rationale_parts.append("退货申请需人工确认商品状态和退货条件")
        elif issue == "warranty":
            required_evidence = ["故障现象说明", "商品序列号或 SKU"]
            rationale_parts.append("质保申请需核对商品与故障信息")
        else:
            rationale_parts.append("未命中专用政策，进入人工售后复核")

        if not eligible:
            action = "manual_resolution_review"
            next_steps.append("由人工确认是否存在例外处理条件")
        else:
            next_steps.append("人工审批处理方案后再执行任何敏感操作")

        return PolicyEvaluation(
            policy_code=f"V1-{issue.upper()}",
            issue_type=issue,
            proposed_action=action,
            eligible_for_review=eligible,
            requires_approval=action in SENSITIVE_ACTIONS,
            rationale="；".join(rationale_parts),
            required_evidence=required_evidence,
            next_steps=next_steps,
        )
