from collections.abc import Awaitable, Callable

from langchain_openai import ChatOpenAI

from app.agents.contracts import AftersalesDecision
from app.agents.supervisor import SupervisorRouter
from app.config import settings
from app.nodes.order import extract_order_no
from app.tools.order_tools import GetOrderStatusTool
from app.tools.policy_tools import EvaluateAftersalesPolicyTool


AFTERSALES_PROMPT = """You classify an ecommerce aftersales request.
Return a structured decision only. Supported issue types are damaged, wrong_item, lost,
delayed, cancel_request, return_request, warranty, address_change, and other. Supported requested actions
are refund, replacement, compensation, cancel, return, warranty_service, change_address, and investigate.
Describe only what the user claims; do not treat a claim as a verified order or shipment fact.
"""


AftersalesModelClassifier = Callable[[str], Awaitable[AftersalesDecision]]


class AftersalesAgent:
    def __init__(
        self,
        order_tool: GetOrderStatusTool,
        policy_tool: EvaluateAftersalesPolicyTool,
        *,
        model_classifier: AftersalesModelClassifier | None = None,
        llm_enabled: bool | None = None,
    ) -> None:
        self.order_tool = order_tool
        self.policy_tool = policy_tool
        self._model_classifier = model_classifier
        self._llm_enabled = bool(settings.API_KEY) if llm_enabled is None else llm_enabled

    async def classify(self, message: str) -> AftersalesDecision:
        local_decision = self._classify_with_rules(message)
        # MIGRATION: address PII and explicit cancellation remain deterministic local classifications.
        if local_decision.issue_type in {"address_change", "cancel_request"}:
            return local_decision
        if self._llm_enabled:
            try:
                classifier = self._model_classifier or self._classify_with_llm
                decision = await classifier(message)
                return decision.model_copy(update={"source": "llm"})
            except Exception:
                pass
        return self._classify_with_rules(message)

    async def run(
        self,
        *,
        message: str,
        user_id: int,
        shop_id: str,
        request_id: str,
    ) -> dict:
        decision = await self.classify(message)
        order_no = extract_order_no(message)
        base_result = {
            "dispatched_to": "aftersales_handling",
            "issue_type": decision.issue_type,
        }
        if order_no is None:
            return {
                **base_result,
                "order_id": None,
                "order_facts": None,
                "shipment_facts": None,
                "policy_result": None,
                "proposed_action": None,
                "requires_approval": False,
                "answer": "请提供格式为 VLT-YYYY-NNNN 的订单号，我才能核对订单、物流和售后政策。",
                "tool_trace": [],
            }

        order_result = self.order_tool.execute(
            order_no=order_no,
            user_id=user_id,
            shop_id=shop_id,
            request_id=request_id,
        )
        if order_result.order is None:
            return {
                **base_result,
                "order_id": order_no,
                "order_facts": None,
                "shipment_facts": None,
                "policy_result": None,
                "proposed_action": None,
                "requires_approval": False,
                "answer": "订单不存在或无权访问，无法继续评估售后方案。",
                "tool_trace": [order_result.trace],
            }

        policy_result = self.policy_tool.execute(
            decision=decision,
            order=order_result.order,
            shipment=order_result.shipment,
            request_id=request_id,
            step=2,
        )
        policy = policy_result.policy
        return {
            **base_result,
            "order_id": order_no,
            "order_facts": order_result.order,
            "shipment_facts": order_result.shipment,
            "policy_result": policy,
            "proposed_action": policy["proposed_action"],
            "requires_approval": policy["requires_approval"],
            "answer": self._build_answer(
                decision=decision,
                order=order_result.order,
                shipment=order_result.shipment,
                policy=policy,
            ),
            "tool_trace": [order_result.trace, policy_result.trace],
        }

    async def _classify_with_llm(self, message: str) -> AftersalesDecision:
        model = ChatOpenAI(
            api_key=settings.API_KEY,
            base_url=settings.BASE_URL,
            model=settings.MODEL,
            temperature=0,
            timeout=30,
            max_retries=1,
        )
        structured_model = model.with_structured_output(
            AftersalesDecision,
            method="function_calling",
        )
        result = await structured_model.ainvoke(
            [("system", AFTERSALES_PROMPT), ("human", message)]
        )
        if not isinstance(result, AftersalesDecision):
            result = AftersalesDecision.model_validate(result)
        return result

    @staticmethod
    def _classify_with_rules(message: str) -> AftersalesDecision:
        text = message.casefold()
        address_change = SupervisorRouter._is_address_change(message)
        explicit_cancel = SupervisorRouter._is_explicit_cancel(message)
        issue_rules = (
            ("wrong_item", ("错发", "发错", "错误商品", "wrong item")),
            ("damaged", ("破损", "损坏", "坏了", "damaged", "broken")),
            ("lost", ("丢件", "丢失", "没收到", "lost", "missing package")),
            ("delayed", ("延误", "太慢", "未按时", "delayed", "late")),
            ("return_request", ("退货", "return")),
            ("warranty", ("保修", "质保", "warranty")),
        )
        if address_change:
            issue_type = "address_change"
        elif explicit_cancel:
            issue_type = "cancel_request"
        else:
            issue_type = next(
                (name for name, keywords in issue_rules if any(keyword in text for keyword in keywords)),
                "other",
            )

        action_rules = (
            ("refund", ("退款", "refund")),
            ("replacement", ("换货", "补发", "replacement", "replace")),
            ("compensation", ("赔偿", "补偿", "compensation")),
            ("return", ("退货", "return")),
            ("warranty_service", ("保修", "质保", "warranty")),
        )
        if address_change:
            requested_action = "change_address"
        elif explicit_cancel:
            requested_action = "cancel"
        else:
            requested_action = next(
                (name for name, keywords in action_rules if any(keyword in text for keyword in keywords)),
                "investigate",
            )
        return AftersalesDecision(
            issue_type=issue_type,
            requested_action=requested_action,
            summary=f"用户陈述的售后问题类型为 {issue_type}，请求动作是 {requested_action}",
            source="rule_fallback",
        )

    @staticmethod
    def _build_answer(
        *,
        decision: AftersalesDecision,
        order: dict,
        shipment: dict | None,
        policy: dict,
    ) -> str:
        shipment_exception = shipment.get("exception_type", "none") if shipment else "none"
        lines = [
            f"已核对订单 {order['order_no']}，当前订单状态为 {order['status']}。",
            f"用户陈述的问题类型：{decision.issue_type}；物流系统记录的异常类型：{shipment_exception}。",
            f"建议处理动作：{policy['proposed_action']}。",
            f"政策依据：{policy['rationale']}。",
        ]
        if policy["required_evidence"]:
            lines.append("需要补充：" + "、".join(policy["required_evidence"]) + "。")
        lines.append("下一步：" + "；".join(policy["next_steps"]) + "。")
        if policy["requires_approval"]:
            lines.append("该动作仅为待审批方案，当前未执行退款、补偿、取消、退货、换货或地址变更，也未保存任何地址。")
        return "\n".join(lines)
