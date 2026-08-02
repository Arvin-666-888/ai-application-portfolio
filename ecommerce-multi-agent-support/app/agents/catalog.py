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
price, its ISO currency code when explicit (USD/EUR/GBP), and required charging power when
user provides them. Do not invent constraints or convert currencies.
"""


CatalogModelExtractor = Callable[[str], Awaitable[CatalogSearchDecision]]


SHOP_CURRENCIES = {"shop-us": "USD", "shop-eu": "EUR", "shop-uk": "GBP"}
CURRENCY_ALIASES = {"$": "USD", "€": "EUR", "£": "GBP", "usd": "USD", "eur": "EUR", "gbp": "GBP"}


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

    async def run(self, *, message: str, shop_id: str, request_id: str) -> dict:
        decision = await self.extract_filters(message)
        expected_currency = SHOP_CURRENCIES.get(shop_id.casefold())
        if decision.budget_currency and decision.budget_currency != expected_currency:
            # MIGRATION: price filters are shop-local; cross-currency budgets fail closed without FX.
            return {
                "dispatched_to": "product_inquiry",
                "product_filters": decision.model_dump(mode="json"),
                "products": [],
                "answer": (
                    f"当前店铺商品使用 {expected_currency}，但预算指定为 {decision.budget_currency}。"
                    "系统不进行汇率换算，请改用当前店铺币种提供预算。"
                ),
                "tool_trace": [],
            }
        # MIGRATION: only the historically valid US VC alias is mapped; EU/UK return no fabricated SKU.
        if decision.keyword and re.fullmatch(r"vc-[a-z]{3}-\d{4}", decision.keyword, re.IGNORECASE):
            if shop_id.casefold() == "shop-us":
                decision = decision.model_copy(
                    update={"keyword": f"SHOP-US-{decision.keyword[3:].upper()}"}
                )
            else:
                return {
                    "dispatched_to": "product_inquiry",
                    "product_filters": decision.model_dump(mode="json"),
                    "products": [],
                    "answer": "旧版 VC SKU 仅兼容历史 US 商品；请提供当前店铺的 SHOP-EU 或 SHOP-UK SKU。",
                    "tool_trace": [],
                }
        tool_result = self.tool.execute(decision, shop_id=shop_id, request_id=request_id)
        products = tool_result.products
        return {
            "dispatched_to": "product_inquiry",
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
            r"(?:预算|价格)?\s*(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*元(?:以内|以下|之内)",
            r"(?:under|below|less than)\s*(?P<currency>usd|eur|gbp|[$€£¥￥])?\s*(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)",
            r"(?P<currency>usd|eur|gbp|[$€£¥￥])\s*(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:or less|max|以内|以下)?",
            r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<currency>usd|eur|gbp|[$€£¥￥])\s*(?:or less|max|以内|以下)",
        )
        max_price = None
        budget_currency = None
        for pattern in price_patterns:
            match = re.search(pattern, text)
            if match:
                max_price = Decimal(match.group("amount").replace(",", ""))
                currency_token = match.groupdict().get("currency")
                budget_currency = CURRENCY_ALIASES.get(currency_token.casefold()) if currency_token else None
                break

        power_match = re.search(r"(\d{1,3})\s*(?:w|瓦)", text)
        power_w = int(power_match.group(1)) if power_match else None

        # MIGRATION: accept both legacy VC SKUs and shop-prefixed US/EU/UK SKUs.
        sku_match = re.search(
            r"\b(?:vc|shop-(?:us|eu|uk))-[a-z]{3}-\d{4}\b",
            text,
            flags=re.IGNORECASE,
        )
        keyword = sku_match.group(0).upper() if sku_match else None
        return CatalogSearchDecision(
            keyword=keyword,
            category=category,
            max_price=max_price,
            budget_currency=budget_currency,
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
                f"- {product['name']}（{product['sku']}）：{product['currency']} {product['price']}，库存 {product['stock']}{suffix}"
            )
        if decision.power_w is not None:
            lines.append(f"以上结果均满足 {decision.power_w}W 规格要求。")
        return "\n".join(lines)
