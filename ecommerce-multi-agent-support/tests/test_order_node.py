from datetime import datetime

import pytest

from app.adapters.sqlite import SQLiteOrderRepository, SQLiteShipmentRepository
from app.database import SessionLocal
from app.nodes.order import OrderStatusNode, extract_order_no
from app.tools import GetOrderStatusTool


def build_order_node(db):
    return OrderStatusNode(
        GetOrderStatusTool(
            SQLiteOrderRepository(db),
            SQLiteShipmentRepository(db),
        )
    )


def test_extract_order_no_is_deterministic():
    assert extract_order_no("查询 vlt-2026-0001 的物流") == "VLT-2026-0001"
    assert extract_order_no("没有提供编号") is None


@pytest.mark.parametrize(
    ("timezone_name", "expected_hour", "expected_offset"),
    [
        ("America/Los_Angeles", 0, "-08:00"),
        ("Europe/Berlin", 9, "+01:00"),
        ("Europe/London", 8, "+00:00"),
    ],
)
def test_order_node_treats_naive_datetime_as_utc_before_iana_conversion(
    timezone_name, expected_hour, expected_offset
):
    converted = OrderStatusNode._local_time("2026-01-15T08:00:00", timezone_name)

    assert converted is not None
    parsed = datetime.fromisoformat(converted)
    assert parsed.hour == expected_hour
    assert converted.endswith(expected_offset)


def test_order_node_returns_owned_order_and_shipment(client):
    with SessionLocal() as db:
        result = build_order_node(db).run(
            message="订单 VLT-2026-0001 到哪里了",
            user_id=1,
            shop_id="shop-us",
            timezone="America/Los_Angeles",
            request_id="req_order_owned",
        )

    assert result["order_facts"]["order_no"] == "VLT-2026-0001"
    assert result["shipment_facts"]["order_no"] == "VLT-2026-0001"
    assert "VLT-2026-0001" in result["answer"]
    assert result["tool_trace"][0]["tool"] == "get_order_status"
    assert result["tool_trace"][0]["success"] is True
    assert "user_id" not in result["tool_trace"][0]["arguments"]


def test_order_node_blocks_another_users_order(client):
    with SessionLocal() as db:
        result = build_order_node(db).run(
            message="订单 VLT-2026-0002 到哪里了",
            user_id=1,
            shop_id="shop-us",
            timezone="America/Los_Angeles",
            request_id="req_order_blocked",
        )

    assert result["order_facts"] is None
    assert result["shipment_facts"] is None
    assert result["answer"] == "订单不存在或无权访问，请检查订单号是否属于当前账号。"
    assert result["tool_trace"][0]["success"] is False
    assert result["tool_trace"][0]["result_count"] == 0


def test_order_node_requests_order_number_without_calling_tool(client):
    with SessionLocal() as db:
        result = build_order_node(db).run(
            message="我的订单到哪里了",
            user_id=1,
            shop_id="shop-us",
            timezone="America/Los_Angeles",
            request_id="req_missing_order_no",
        )

    assert result["order_id"] is None
    assert result["order_facts"] is None
    assert result["shipment_facts"] is None
    assert "请提供" in result["answer"]
    assert result["tool_trace"] == []
