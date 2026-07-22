import asyncio
from decimal import Decimal

from app.adapters.sqlite import SQLiteCatalogRepository
from app.agents.catalog import CatalogAgent
from app.database import SessionLocal
from app.tools import SearchProductsTool


def test_catalog_agent_extracts_category_budget_and_power(client):
    with SessionLocal() as db:
        agent = CatalogAgent(
            SearchProductsTool(SQLiteCatalogRepository(db)),
            llm_enabled=False,
        )
        decision = asyncio.run(agent.extract_filters("推荐一款 300 元以内的 65W 充电器"))

    assert decision.category == "charger"
    assert decision.max_price == Decimal("300")
    assert decision.power_w == 65
    assert decision.source == "rule_fallback"


def test_catalog_agent_extracts_power_without_space_before_chinese_text(client):
    with SessionLocal() as db:
        agent = CatalogAgent(
            SearchProductsTool(SQLiteCatalogRepository(db)),
            llm_enabled=False,
        )
        decision = asyncio.run(agent.extract_filters("推荐300元以内的65W充电器"))

    assert decision.power_w == 65


def test_catalog_agent_returns_only_matching_repository_facts(client):
    with SessionLocal() as db:
        repository = SQLiteCatalogRepository(db)
        agent = CatalogAgent(SearchProductsTool(repository), llm_enabled=False)
        result = asyncio.run(
            agent.run(
                message="推荐一款 300 元以内的 65W 充电器",
                request_id="req_catalog_test",
            )
        )

        assert result["products"]
        for returned in result["products"]:
            stored = repository.get_by_sku(returned["sku"])
            assert stored is not None
            assert returned["price"] == str(stored.price)
            assert returned["stock"] == stored.stock
            assert returned["specifications"]["power_w"] == 65
            assert stored.price <= Decimal("300")

    trace = result["tool_trace"][0]
    assert trace["tool"] == "search_products"
    assert trace["success"] is True
    assert trace["result_count"] == len(result["products"])


def test_catalog_agent_returns_clear_empty_result(client):
    with SessionLocal() as db:
        agent = CatalogAgent(
            SearchProductsTool(SQLiteCatalogRepository(db)),
            llm_enabled=False,
        )
        result = asyncio.run(
            agent.run(
                message="推荐一款 10 元以内的 65W 充电器",
                request_id="req_no_match",
            )
        )

    assert result["products"] == []
    assert "没有找到" in result["answer"]


def test_catalog_model_failure_uses_rule_filters(client):
    async def failing_extractor(_):
        raise RuntimeError("model unavailable")

    with SessionLocal() as db:
        agent = CatalogAgent(
            SearchProductsTool(SQLiteCatalogRepository(db)),
            model_extractor=failing_extractor,
            llm_enabled=True,
        )
        decision = asyncio.run(agent.extract_filters("200 元以内的充电宝"))

    assert decision.category == "power_bank"
    assert decision.max_price == Decimal("200")
    assert decision.source == "rule_fallback"
