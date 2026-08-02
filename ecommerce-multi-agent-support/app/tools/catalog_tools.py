from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter

from app.agents.contracts import CatalogSearchDecision
from app.domain.models import Product
from app.ports import CatalogRepository


@dataclass(frozen=True, slots=True)
class SearchProductsToolResult:
    products: list[dict]
    trace: dict


class SearchProductsTool:
    name = "search_products"

    def __init__(self, repository: CatalogRepository):
        self.repository = repository

    def execute(
        self,
        decision: CatalogSearchDecision,
        *,
        shop_id: str,
        request_id: str,
    ) -> SearchProductsToolResult:
        started = perf_counter()
        candidates = self.repository.search(
            shop_id=shop_id,
            keyword=decision.keyword,
            category=decision.category,
            max_price=decision.max_price,
            in_stock_only=decision.in_stock_only,
            limit=50,
        )
        matched = [
            product
            for product in candidates
            if self._matches_specs(product, power_w=decision.power_w)
        ][: decision.limit]
        products = [self._serialize(product) for product in matched]
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        trace = {
            "step": 1,
            "request_id": request_id,
            "tool": self.name,
            "arguments": {
                "keyword": decision.keyword,
                "category": decision.category,
                "max_price": str(decision.max_price) if decision.max_price is not None else None,
                "budget_currency": decision.budget_currency,
                "power_w": decision.power_w,
                "in_stock_only": decision.in_stock_only,
                "limit": decision.limit,
            },
            "success": True,
            "result_count": len(products),
            "duration_ms": elapsed_ms,
        }
        return SearchProductsToolResult(products=products, trace=trace)

    @staticmethod
    def _matches_specs(product: Product, *, power_w: int | None) -> bool:
        if power_w is None:
            return True
        return product.specifications.get("power_w") == power_w

    @staticmethod
    def _serialize(product: Product) -> dict:
        return {
            "id": product.id,
            "sku": product.sku,
            "name": product.name,
            "category": product.category,
            "price": str(product.price),
            "currency": product.currency,
            "stock": product.stock,
            "specifications": product.specifications,
        }
