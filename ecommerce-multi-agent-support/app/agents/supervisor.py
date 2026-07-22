from collections.abc import Awaitable, Callable

from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.agents.contracts import RouteDecision
from app.config import settings


SUPERVISOR_PROMPT = """You are the routing supervisor for an ecommerce support system.
Classify the user's latest message into exactly one route:
- catalog: product discovery, comparison, price, specification, compatibility, or stock questions.
- order: order status or shipment tracking questions without a complaint or requested remedy.
- aftersales: return, refund, cancellation, damage, loss, wrong item, delay complaint, warranty claim, or compensation.
- unsupported: unrelated requests or requests outside ecommerce support.

Return only the structured decision. Do not answer the user's business question.
"""


ModelRouter = Callable[[str], Awaitable[RouteDecision]]


class SupervisorRouter:
    def __init__(
        self,
        *,
        model_router: ModelRouter | None = None,
        llm_enabled: bool | None = None,
    ) -> None:
        self._model_router = model_router
        self._llm_enabled = bool(settings.API_KEY) if llm_enabled is None else llm_enabled

    async def decide(self, message: str) -> RouteDecision:
        normalized = message.strip()
        if not normalized:
            return RouteDecision(
                route="unsupported",
                confidence=1.0,
                reason="The message is empty.",
                source="rule_fallback",
            )

        if self._llm_enabled:
            try:
                router = self._model_router or self._route_with_llm
                decision = await router(normalized)
                return decision.model_copy(update={"source": "llm"})
            except Exception:
                # Routing must remain available when the model or its structured output fails.
                pass

        return self._route_with_rules(normalized)

    async def _route_with_llm(self, message: str) -> RouteDecision:
        model = ChatOpenAI(
            api_key=settings.API_KEY,
            base_url=settings.BASE_URL,
            model=settings.MODEL,
            temperature=0,
            timeout=30,
            max_retries=1,
        )
        structured_model = model.with_structured_output(RouteDecision, method="function_calling")
        result = await structured_model.ainvoke(
            [
                ("system", SUPERVISOR_PROMPT),
                ("human", message),
            ]
        )
        if not isinstance(result, RouteDecision):
            result = RouteDecision.model_validate(result)
        return result

    @staticmethod
    def _route_with_rules(message: str) -> RouteDecision:
        text = message.casefold()
        unsupported_keywords = (
            "股票", "基金", "期货", "证券", "量子力学", "快速排序", "写程序", "写代码",
            "写一首", "写诗", "天气", "stock price", "programming", "write code", "poem",
        )
        aftersales_keywords = (
            "退款", "退货", "换货", "取消订单", "破损", "损坏", "坏了", "错发", "少发",
            "丢件", "丢失", "延误", "太慢", "投诉", "赔偿", "补偿", "保修", "质保",
            "refund", "return", "replace", "cancel", "damaged", "broken", "wrong item",
            "lost", "delayed", "complaint", "compensation", "warranty",
        )
        order_keywords = (
            "订单", "物流", "快递", "运单", "包裹", "发货", "到哪", "送达", "签收",
            "order", "shipment", "shipping", "tracking", "package", "delivery",
        )
        catalog_keywords = (
            "推荐", "商品", "产品", "价格", "多少钱", "对比", "比较", "参数", "规格", "兼容",
            "库存", "有货", "充电器", "充电宝", "数据线", "耳机", "适配器", "支架",
            "recommend", "product", "price", "compare", "spec", "compatible", "stock",
            "charger", "power bank", "cable", "earbuds", "adapter",
        )

        if any(keyword in text for keyword in unsupported_keywords):
            return RouteDecision(
                route="unsupported",
                confidence=0.95,
                reason="Matched an explicitly unsupported non-commerce domain.",
                source="rule_fallback",
            )
        if any(keyword in text for keyword in aftersales_keywords):
            return RouteDecision(
                route="aftersales",
                confidence=0.9,
                reason="Matched an aftersales complaint or remedy request.",
                source="rule_fallback",
            )
        if any(keyword in text for keyword in order_keywords):
            return RouteDecision(
                route="order",
                confidence=0.85,
                reason="Matched an order or shipment status question.",
                source="rule_fallback",
            )
        if any(keyword in text for keyword in catalog_keywords):
            return RouteDecision(
                route="catalog",
                confidence=0.85,
                reason="Matched a product discovery or comparison question.",
                source="rule_fallback",
            )
        return RouteDecision(
            route="unsupported",
            confidence=0.7,
            reason="No supported ecommerce intent was detected.",
            source="rule_fallback",
        )
