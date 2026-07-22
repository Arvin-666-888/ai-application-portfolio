"""Exercise the V1 API without starting an external server."""
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from app.main import app


def login_headers_for(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "DemoPass123!"},
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def main() -> None:
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "demo_user_01", "password": "DemoPass123!"},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        products = client.get("/api/v1/products", headers=headers, params={"category": "charger", "limit": 3})
        owned_order = client.get("/api/v1/orders/VLT-2026-0001", headers=headers)
        blocked_order = client.get("/api/v1/orders/VLT-2026-0002", headers=headers)
        shipment = client.get("/api/v1/orders/VLT-2026-0001/shipment", headers=headers)
        routing_cases = {
            message: client.post(
                "/api/v1/routing/preview",
                headers=headers,
                json={"message": message, "session_id": "smoke-session"},
            ).json()["dispatched_to"]
            for message in (
                "推荐一款 300 元以内的充电器",
                "订单 VLT-2026-0001 到哪里了",
                "包裹损坏了，我要退款",
                "帮我写一首诗",
            )
        }
        catalog_chat = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "推荐一款 300 元以内的 65W 充电器",
                "session_id": "smoke-session",
            },
        )
        catalog_chat.raise_for_status()
        catalog_data = catalog_chat.json()
        order_chat = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "订单 VLT-2026-0001 到哪里了",
                "session_id": "smoke-session",
            },
        )
        order_chat.raise_for_status()
        order_data = order_chat.json()
        blocked_order_chat = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "订单 VLT-2026-0002 到哪里了",
                "session_id": "smoke-session",
            },
        )
        blocked_order_chat.raise_for_status()
        blocked_order_data = blocked_order_chat.json()
        aftersales_chat = client.post(
            "/api/v1/chat",
            headers=login_headers_for(client, "demo_user_03"),
            json={
                "message": "订单 VLT-2026-0015 的商品破损了，我要退款",
                "session_id": "smoke-session",
            },
        )
        aftersales_chat.raise_for_status()
        aftersales_data = aftersales_chat.json()
        claim_only_chat = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "订单 VLT-2026-0001 的商品破损了，需要换货",
                "session_id": "smoke-session",
            },
        )
        claim_only_chat.raise_for_status()
        claim_only_data = claim_only_chat.json()

        print(
            {
                "health": client.get("/health").json(),
                "product_count": len(products.json()),
                "owned_order_status": owned_order.status_code,
                "blocked_order_status": blocked_order.status_code,
                "shipment_status": shipment.status_code,
                "routing": routing_cases,
                "catalog_chat": {
                    "route": catalog_data["route"],
                    "result_count": len(catalog_data["products"]),
                    "all_65w": all(
                        item["specifications"].get("power_w") == 65
                        for item in catalog_data["products"]
                    ),
                    "tool": catalog_data["tool_trace"][0]["tool"],
                },
                "order_chat": {
                    "route": order_data["route"],
                    "order_no": order_data["order_facts"]["order_no"],
                    "shipment_found": order_data["shipment_facts"] is not None,
                    "tool": order_data["tool_trace"][0]["tool"],
                },
                "blocked_order_chat": {
                    "facts_exposed": blocked_order_data["order_facts"] is not None,
                    "shipment_exposed": blocked_order_data["shipment_facts"] is not None,
                    "tool_success": blocked_order_data["tool_trace"][0]["success"],
                },
                "aftersales_chat": {
                    "route": aftersales_data["route"],
                    "issue_type": aftersales_data["issue_type"],
                    "shipment_exception": aftersales_data["shipment_facts"]["exception_type"],
                    "proposed_action": aftersales_data["proposed_action"],
                    "requires_approval": aftersales_data["requires_approval"],
                    "tools": [item["tool"] for item in aftersales_data["tool_trace"]],
                },
                "claim_only_chat": {
                    "user_claim": claim_only_data["issue_type"],
                    "system_exception": claim_only_data["shipment_facts"]["exception_type"],
                    "requires_evidence": bool(
                        claim_only_data["policy_result"]["required_evidence"]
                    ),
                },
            }
        )


if __name__ == "__main__":
    main()
