import asyncio
from decimal import Decimal

import pytest

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


@pytest.mark.parametrize(
    ("message", "expected_budget"),
    [
        ("65W charger under $1,200", Decimal("1200")),
        ("65W Ladegerät unter €1,180", Decimal("1180")),
        ("65W charger £1,150 max", Decimal("1150")),
        ("budget 175 USD or less charger", Decimal("175")),
        ("EUR 160以内充电器", Decimal("160")),
        ("140 GBP以下charger", Decimal("140")),
    ],
)
def test_catalog_agent_extracts_three_currency_prefix_and_suffix_budgets(message, expected_budget, client):
    with SessionLocal() as db:
        agent = CatalogAgent(SearchProductsTool(SQLiteCatalogRepository(db)), llm_enabled=False)
        decision = asyncio.run(agent.extract_filters(message))

    assert decision.max_price == expected_budget


@pytest.mark.parametrize(
    ("message", "expected_currency"),
    [("$1,200以内充电器", "USD"), ("EUR 1,100以下充电器", "EUR"), ("950 GBP以下charger", "GBP")],
)
def test_catalog_agent_extracts_explicit_budget_currency(message, expected_currency, client):
    with SessionLocal() as db:
        agent = CatalogAgent(SearchProductsTool(SQLiteCatalogRepository(db)), llm_enabled=False)
        decision = asyncio.run(agent.extract_filters(message))

    assert decision.budget_currency == expected_currency


@pytest.mark.parametrize(
    ("message", "expected_sku"),
    [
        ("查询 VC-CHA-0001", "VC-CHA-0001"),
        ("查询 SHOP-US-CHA-0001", "SHOP-US-CHA-0001"),
        ("查询 shop-eu-cha-0002", "SHOP-EU-CHA-0002"),
        ("查询 SHOP-UK-CHA-0003", "SHOP-UK-CHA-0003"),
    ],
)
def test_catalog_agent_accepts_legacy_and_shop_prefixed_skus(message, expected_sku, client):
    with SessionLocal() as db:
        agent = CatalogAgent(SearchProductsTool(SQLiteCatalogRepository(db)), llm_enabled=False)
        decision = asyncio.run(agent.extract_filters(message))

    assert decision.keyword == expected_sku


def test_catalog_agent_resolves_legacy_sku_inside_trusted_shop(client):
    with SessionLocal() as db:
        agent = CatalogAgent(SearchProductsTool(SQLiteCatalogRepository(db)), llm_enabled=False)
        result = asyncio.run(
            agent.run(
                shop_id="shop-us",
                message="查询商品 VC-CHA-0001",
                request_id="req_legacy_sku",
            )
        )

    assert [product["sku"] for product in result["products"]] == ["SHOP-US-CHA-0001"]
    assert result["product_filters"]["keyword"] == "SHOP-US-CHA-0001"


@pytest.mark.parametrize("shop_id", ["shop-eu", "shop-uk"])
def test_catalog_agent_does_not_fabricate_legacy_sku_for_non_us_shop(shop_id, client):
    with SessionLocal() as db:
        agent = CatalogAgent(SearchProductsTool(SQLiteCatalogRepository(db)), llm_enabled=False)
        result = asyncio.run(
            agent.run(shop_id=shop_id, message="查询商品 VC-CHA-0001", request_id="req_legacy")
        )

    assert result["products"] == []
    assert result["tool_trace"] == []
    assert "仅兼容历史 US 商品" in result["answer"]


@pytest.mark.parametrize(
    ("shop_id", "message", "shop_currency", "budget_currency"),
    [
        ("shop-us", "推荐 EUR 200 以下的充电器", "USD", "EUR"),
        ("shop-eu", "推荐 $200 以下的充电器", "EUR", "USD"),
        ("shop-uk", "推荐 EUR 200 以下的充电器", "GBP", "EUR"),
    ],
)
def test_catalog_agent_fails_closed_on_cross_currency_budget(
    shop_id, message, shop_currency, budget_currency, client
):
    with SessionLocal() as db:
        agent = CatalogAgent(SearchProductsTool(SQLiteCatalogRepository(db)), llm_enabled=False)
        result = asyncio.run(agent.run(shop_id=shop_id, message=message, request_id="req_fx"))

    assert result["products"] == []
    assert result["tool_trace"] == []
    assert result["product_filters"]["budget_currency"] == budget_currency
    assert shop_currency in result["answer"]
    assert "不进行汇率换算" in result["answer"]


def test_catalog_agent_returns_only_matching_repository_facts(client):
    with SessionLocal() as db:
        repository = SQLiteCatalogRepository(db)
        agent = CatalogAgent(SearchProductsTool(repository), llm_enabled=False)
        result = asyncio.run(
            agent.run(
                shop_id="shop-us",
                message="推荐一款 300 元以内的 65W 充电器",
                request_id="req_catalog_test",
            )
        )

        assert result["products"]
        for returned in result["products"]:
            stored = repository.get_by_sku(shop_id="shop-us", sku=returned["sku"])
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
                shop_id="shop-us",
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
