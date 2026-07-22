import re
from collections.abc import Awaitable, Callable
from decimal import Decimal

from langchain_openai import ChatOpenAI

from app.agents.contracts import CatalogSearchDecision
from app.config import settings
from app.tools.catalog_tools import SearchProductsTool


CATALOG_PROMPT = """You extract product search filters for the VoltCore catalog.
Return a structured search decision only. Supported categories are charger, power_bank,
cable, hub, wireless_charger, accessory, audio, and adapter. Extract an explicit maximum
price and required charging power when the user provides them. Do not invent constraints.
"""


CatalogModelExtractor = Callable[[str], Awaitable[CatalogSearchDecision]]


class CatalogAgent:
    def __init__(
        self,
        tool: SearchProductsTool,
        *,
        model_extractor: CatalogModelExtractor | None = None,
        llm_enabled: bool | None = None,
    ) -> None:
        self.tool = tool
        self._model_extractor = model_extractor
        self._llm_enabled = bool(settings.API_KEY) if llm_enabled is None else llm_enabled

    async def extract_filters(self, message: str) -> CatalogSearchDecision:
        if self._llm_enabled:
            try:
                extractor = self._model_extractor or self._extract_with_llm
                decision = await extractor(message)
                return decision.model_copy(update={"source": "llm"})
            except Exception:
                pass
        return self._extract_with_rules(message)

    async def run(self, *, message: str, request_id: str) -> dict:
        decision = await self.extract_filters(message)
        tool_result = self.tool.execute(decision, request_id=request_id)
        products = tool_result.products
        return {
            "dispatched_to": "catalog",
            "product_filters": decision.model_dump(mode="json"),
            "products": products,
            "answer": self._build_answer(products, decision),
            "tool_trace": [tool_result.trace],
        }

    async def _extract_with_llm(self, message: str) -> CatalogSearchDecision:
        model = ChatOpenAI(
            api_key=settings.API_KEY,
            base_url=settings.BASE_URL,
            model=settings.MODEL,
            temperature=0,
            timeout=30,
            max_retries=1,
        )
        structured_model = model.with_structured_output(CatalogSearchDecision, method="function_calling")
        result = await structured_model.ainvoke(
            [("system", CATALOG_PROMPT), ("human", message)]
        )
        if not isinstance(result, CatalogSearchDecision):
            result = CatalogSearchDecision.model_validate(result)
        return result

    @staticmethod
    def _extract_with_rules(message: str) -> CatalogSearchDecision:
        text = message.casefold()
        category_aliases = (
            ("wireless_charger", ("无线充电", "wireless charger", "qi2")),
            ("power_bank", ("充电宝", "移动电源", "power bank")),
            ("charger", ("充电器", "充电头", "charger")),
            ("cable", ("数据线", "充电线", "cable")),
            ("hub", ("扩展坞", "集线器", "hub")),
            ("audio", ("耳机", "earbuds", "headphones")),
            ("adapter", ("旅行转换器", "旅行适配器", "travel adapter")),
            ("accessory", ("支架", "stand", "accessory")),
        )
        category = next(
            (name for name, aliases in category_aliases if any(alias in text for alias in aliases)),
            None,
        )

        price_patterns = (
            r"(?:预算|价格)?\s*(\d+(?:\.\d+)?)\s*元?(?:以内|以下|之内)",
            r"(?:under|below|less than)\s*[$¥￥]?\s*(\d+(?:\.\d+)?)",
            r"[$¥￥]\s*(\d+(?:\.\d+)?)\s*(?:or less|max)?",
        )
        max_price = None
        for pattern in price_patterns:
            match = re.search(pattern, text)
            if match:
                max_price = Decimal(match.group(1))
                break

        power_match = re.search(r"(\d{1,3})\s*(?:w|瓦)", text)
        power_w = int(power_match.group(1)) if power_match else None

        sku_match = re.search(r"\bvc-[a-z]{3}-\d{4}\b", text, flags=re.IGNORECASE)
        keyword = sku_match.group(0).upper() if sku_match else None
        return CatalogSearchDecision(
            keyword=keyword,
            category=category,
            max_price=max_price,
            power_w=power_w,
            in_stock_only=True,
            limit=5,
            source="rule_fallback",
        )

    @staticmethod
    def _build_answer(products: list[dict], decision: CatalogSearchDecision) -> str:
        if not products:
            return "没有找到同时满足这些条件的在售商品。你可以放宽预算或规格后再试。"

        lines = [f"找到 {len(products)} 款符合条件的商品："]
        for product in products:
            specs = product["specifications"]
            spec_parts = []
            if "power_w" in specs:
                spec_parts.append(f"{specs['power_w']}W")
            if "ports" in specs:
                spec_parts.append(f"{specs['ports']} 个接口")
            spec_text = "，".join(spec_parts)
            suffix = f"，{spec_text}" if spec_text else ""
            lines.append(
                f"- {product['name']}（{product['sku']}）：¥{product['price']}，库存 {product['stock']}{suffix}"
            )
        if decision.power_w is not None:
            lines.append(f"以上结果均满足 {decision.power_w}W 规格要求。")
        return "\n".join(lines)
