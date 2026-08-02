from collections.abc import Awaitable, Callable
import re

from langchain_openai import ChatOpenAI

from app.agents.contracts import RouteDecision
from app.config import settings


SUPERVISOR_PROMPT = """You are the routing supervisor for an ecommerce support system.
Classify the user's latest message into exactly one route:
- product_inquiry: product discovery, comparison, price, specification, compatibility, or stock.
- aftersales_handling: return, refund, cancellation, damage, loss, complaint, warranty, compensation, or delivery-address change. Address changes always belong here.
- order_query: order status questions that do not ask for shipment tracking or a remedy.
- logistics_tracking: shipment, carrier, tracking, package-location, or delivery-time questions without a complaint.
- unsupported: unrelated requests or requests outside ecommerce support.

Return only the structured decision. Do not answer the user's business question.
"""


ModelRouter = Callable[[str], Awaitable[RouteDecision]]


# MIGRATION: deterministic privacy/action boundaries stay synchronized with aftersales rules.
ADDRESS_CHANGE_KEYWORDS = (
    "修改地址", "更改地址", "改地址", "换地址", "地址改", "地址换", "地址变更",
    "修改收货地址", "更改收货地址", "收货地址改",
    "change address", "change shipping address", "update delivery address",
)
ADDRESS_DESTINATION_PATTERN = re.compile(
    r"(?:寄到|送到|寄往|配送到|改寄|改送|ship\s+to|deliver\s+to)\s*"
    r"(?=.{2,})(?:[^，。！？?\n]*?(?:路|街|道|巷|号|室|楼|栋|大厦|公寓|street|st\.?|road|rd\.?|"
    r"avenue|ave\.?|lane|ln\.?|drive|dr\.?|building|apartment|apt\.?|\d{5,6}|1\d{10}))",
    flags=re.IGNORECASE,
)
EXPLICIT_CANCEL_KEYWORDS = (
    "取消订单", "取消这个订单", "订单取消", "撤销订单", "取消购买",
    "不要这个订单", "这个订单不要了", "不想要这个订单", "订单不想要了",
    "cancel order", "cancel my order", "cancel this order", "cancel the order",
)
CANCEL_WITH_ORDER_PATTERN = re.compile(
    r"(?:取消|撤销|cancel).{0,20}\bVLT-\d{4}-\d{4}\b|"
    r"\bVLT-\d{4}-\d{4}\b.{0,20}(?:取消|撤销|cancel)",
    flags=re.IGNORECASE,
)
CANCEL_NEGATION_PATTERNS = (
    re.compile(r"不要.{0,12}(?:订单号|发票).{0,20}(?:订单.{0,8}(?:保留|不要取消)|保留订单)"),
    re.compile(r"(?:订单.{0,8}(?:保留|不要取消)|保留订单).{0,20}不要.{0,12}(?:订单号|发票)"),
)


class SupervisorRouter:
    def __init__(self, *, model_router: ModelRouter | None = None, llm_enabled: bool | None = None) -> None:
        self._model_router = model_router
        self._llm_enabled = bool(settings.API_KEY) if llm_enabled is None else llm_enabled

    async def decide(self, message: str) -> RouteDecision:
        normalized = message.strip()
        if not normalized:
            return RouteDecision(route="unsupported", confidence=1.0, reason="The message is empty.", source="rule_fallback")
        local_decision = self._route_with_rules(normalized)
        # MIGRATION: address PII and explicit cancellation requests never leave the local boundary.
        if self._is_address_change(normalized) or self._is_explicit_cancel(normalized):
            return local_decision
        if self._llm_enabled:
            try:
                router = self._model_router or self._route_with_llm
                decision = await router(normalized)
                return decision.model_copy(update={"source": "llm"})
            except Exception:
                pass
        return self._route_with_rules(normalized)

    async def _route_with_llm(self, message: str) -> RouteDecision:
        model = ChatOpenAI(
            api_key=settings.API_KEY, base_url=settings.BASE_URL, model=settings.MODEL,
            temperature=0, timeout=30, max_retries=1,
        )
        structured_model = model.with_structured_output(RouteDecision, method="function_calling")
        result = await structured_model.ainvoke([("system", SUPERVISOR_PROMPT), ("human", message)])
        return result if isinstance(result, RouteDecision) else RouteDecision.model_validate(result)

    @staticmethod
    def _is_address_change(message: str) -> bool:
        text = message.casefold()
        return (
            any(keyword in text for keyword in ADDRESS_CHANGE_KEYWORDS)
            or ADDRESS_DESTINATION_PATTERN.search(text) is not None
        )

    @staticmethod
    def _is_explicit_cancel(message: str) -> bool:
        text = message.casefold()
        if any(pattern.search(text) for pattern in CANCEL_NEGATION_PATTERNS):
            return False
        return (
            any(keyword in text for keyword in EXPLICIT_CANCEL_KEYWORDS)
            or CANCEL_WITH_ORDER_PATTERN.search(text) is not None
        )

    @staticmethod
    def _route_with_rules(message: str) -> RouteDecision:
        text = message.casefold()
        unsupported_keywords = (
            "股票", "基金", "期货", "证券", "量子力学", "快速排序", "写程序", "写代码",
            "写一首", "写诗", "天气", "stock price", "programming", "write code", "poem",
        )
        aftersales_keywords = (
            "退款", "退货", "换货", "破损", "损坏", "坏了", "错发", "少发",
            "丢件", "丢失", "延误", "太慢", "投诉", "赔偿", "补偿", "保修", "质保",
            "refund", "return", "replace", "damaged", "broken", "wrong item",
            "lost", "delayed", "complaint", "compensation", "warranty",
        )
        logistics_keywords = (
            "物流", "快递", "运单", "包裹", "发货", "到哪", "送到", "送达", "签收", "几号到", "何时到",
            "shipment", "shipping", "tracking", "track order", "track my order", "track this order",
            "package", "delivery", "carrier", "arrive", "arrival", "delivered",
        )
        order_keywords = ("订单", "订单状态", "order", "order status")
        catalog_keywords = (
            "推荐", "商品", "产品", "价格", "多少钱", "对比", "比较", "参数", "规格", "兼容",
            "库存", "有货", "充电器", "充电宝", "数据线", "耳机", "适配器", "支架",
            "recommend", "product", "price", "compare", "spec", "compatible", "stock",
            "charger", "power bank", "cable", "earbuds", "adapter",
        )

        address_change = SupervisorRouter._is_address_change(message)
        explicit_cancel = SupervisorRouter._is_explicit_cancel(message)

        # MIGRATION: local privacy/action intents precede unsupported substrings found inside PII.
        if address_change or explicit_cancel:
            return RouteDecision(route="aftersales_handling", confidence=0.95, reason="Matched a local-only address change or explicit cancellation request.", source="rule_fallback")
        if any(keyword in text for keyword in unsupported_keywords):
            return RouteDecision(route="unsupported", confidence=0.95, reason="Matched an explicitly unsupported non-commerce domain.", source="rule_fallback")
        if any(keyword in text for keyword in aftersales_keywords):
            return RouteDecision(route="aftersales_handling", confidence=0.9, reason="Matched an aftersales request or delivery-address change.", source="rule_fallback")
        if any(keyword in text for keyword in logistics_keywords):
            return RouteDecision(route="logistics_tracking", confidence=0.88, reason="Matched a shipment or logistics tracking question.", source="rule_fallback")
        if any(keyword in text for keyword in order_keywords):
            return RouteDecision(route="order_query", confidence=0.85, reason="Matched an order status question.", source="rule_fallback")
        if any(keyword in text for keyword in catalog_keywords):
            return RouteDecision(route="product_inquiry", confidence=0.85, reason="Matched a product discovery or comparison question.", source="rule_fallback")
        return RouteDecision(route="unsupported", confidence=0.7, reason="No supported ecommerce intent was detected.", source="rule_fallback")
