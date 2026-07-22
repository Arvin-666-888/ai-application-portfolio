# requirements.txt: fastapi==0.139.0, httpx==0.28.1, pydantic==2.13.4
import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import app


DEFAULT_CASES = PROJECT_ROOT / "evals" / "cases.jsonl"
DEFAULT_JSON_REPORT = PROJECT_ROOT / "evals" / "latest_report.json"
DEFAULT_MD_REPORT = PROJECT_ROOT / "docs" / "V1_EVALUATION_REPORT.md"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc
    return cases


def login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "DemoPass123!"},
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def check_case(case: dict[str, Any], data: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    def expect_equal(field: str, expected: Any, actual: Any) -> None:
        if actual != expected:
            failures.append(f"{field}: expected {expected!r}, got {actual!r}")

    expect_equal("route", case["expected_route"], data.get("route"))
    expect_equal("dispatched_to", case["expected_route"], data.get("dispatched_to"))
    expect_equal(
        "tools",
        case.get("expected_tools", []),
        [item.get("tool") for item in data.get("tool_trace", [])],
    )

    if "expected_category" in case:
        expect_equal("category", case["expected_category"], data.get("product_filters", {}).get("category"))
    if "expected_power_w" in case:
        expect_equal("power_w", case["expected_power_w"], data.get("product_filters", {}).get("power_w"))
        for product in data.get("products", []):
            if product.get("specifications", {}).get("power_w") != case["expected_power_w"]:
                failures.append(f"product {product.get('sku')} violates power_w constraint")
    if "max_product_price" in case:
        maximum = Decimal(str(case["max_product_price"]))
        for product in data.get("products", []):
            if Decimal(product["price"]) > maximum:
                failures.append(f"product {product.get('sku')} exceeds max price")
    if "expected_sku" in case:
        if case["expected_sku"] not in [item.get("sku") for item in data.get("products", [])]:
            failures.append(f"expected SKU {case['expected_sku']} not returned")
    if "min_products" in case and len(data.get("products", [])) < case["min_products"]:
        failures.append("product result count below minimum")
    if "max_products" in case and len(data.get("products", [])) > case["max_products"]:
        failures.append("product result count above maximum")

    if "expected_order_no" in case:
        expect_equal("order_no", case["expected_order_no"], (data.get("order_facts") or {}).get("order_no"))
    if "expect_order_facts" in case:
        expect_equal("order_facts_present", case["expect_order_facts"], data.get("order_facts") is not None)
    if "expect_shipment_facts" in case:
        expect_equal("shipment_facts_present", case["expect_shipment_facts"], data.get("shipment_facts") is not None)
    if "expected_issue_type" in case:
        expect_equal("issue_type", case["expected_issue_type"], data.get("issue_type"))
    if "expected_shipment_exception" in case:
        expect_equal(
            "shipment_exception",
            case["expected_shipment_exception"],
            (data.get("shipment_facts") or {}).get("exception_type"),
        )
    if "expected_action" in case:
        expect_equal("proposed_action", case["expected_action"], data.get("proposed_action"))
    if "expected_requires_approval" in case:
        expect_equal("requires_approval", case["expected_requires_approval"], data.get("requires_approval"))
    if "expected_policy_code" in case:
        expect_equal("policy_code", case["expected_policy_code"], (data.get("policy_result") or {}).get("policy_code"))
    if "answer_contains" in case and case["answer_contains"] not in (data.get("answer") or ""):
        failures.append(f"answer does not contain {case['answer_contains']!r}")

    if case.get("security_case"):
        if data.get("order_facts") is not None or data.get("shipment_facts") is not None:
            failures.append("security case exposed order or shipment facts")

    return failures


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# V1 Evaluation Report",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Mode: `{report['mode']}`",
        f"- Cases: `{report['total']}`",
        f"- Passed: `{report['passed']}`",
        f"- Overall pass rate: `{report['pass_rate']:.1%}`",
        f"- Route accuracy: `{report['route_accuracy']:.1%}`",
        f"- Tool selection accuracy: `{report['tool_accuracy']:.1%}`",
        f"- Security cases passed: `{report['security_passed']}/{report['security_total']}`",
        "",
        "## Case Results",
        "",
        "| Case | Route | Result | Details |",
        "|---|---|---|---|",
    ]
    for item in report["results"]:
        details = "; ".join(item["failures"]) if item["failures"] else "OK"
        lines.append(f"| `{item['id']}` | `{item['route']}` | {'PASS' if item['passed'] else 'FAIL'} | {details} |")
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This report validates the deterministic local V1 path. It does not claim cloud-model accuracy, production traffic, or real-platform business outcomes.",
            "",
        ]
    )
    return "\n".join(lines)


def run(cases_path: Path) -> dict[str, Any]:
    cases = load_cases(cases_path)
    results = []
    with TestClient(app) as client:
        headers_by_user: dict[str, dict[str, str]] = {}
        for case in cases:
            username = case["username"]
            if username not in headers_by_user:
                headers_by_user[username] = login(client, username)
            headers = headers_by_user[username]
            response = client.post(
                "/api/v1/chat",
                headers=headers,
                json={"message": case["message"], "session_id": f"eval-{case['id']}"},
            )
            if response.status_code != 200:
                failures = [f"HTTP {response.status_code}: {response.text[:200]}"]
                data = {}
            else:
                data = response.json()
                failures = check_case(case, data)
            results.append(
                {
                    "id": case["id"],
                    "route": data.get("route"),
                    "expected_route": case["expected_route"],
                    "tools": [item.get("tool") for item in data.get("tool_trace", [])],
                    "passed": not failures,
                    "failures": failures,
                    "security_case": bool(case.get("security_case")),
                }
            )

    total = len(results)
    passed = sum(item["passed"] for item in results)
    route_correct = sum(item["route"] == item["expected_route"] for item in results)
    tool_correct = sum(
        item["tools"] == case.get("expected_tools", [])
        for item, case in zip(results, cases)
    )
    security_results = [item for item in results if item["security_case"]]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "local_rule_fallback",
        "total": total,
        "passed": passed,
        "pass_rate": passed / total if total else 0,
        "route_accuracy": route_correct / total if total else 0,
        "tool_accuracy": tool_correct / total if total else 0,
        "security_total": len(security_results),
        "security_passed": sum(item["passed"] for item in security_results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the V1 ecommerce multi-agent evaluation suite.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--md-report", type=Path, default=DEFAULT_MD_REPORT)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.validate_only:
        print(json.dumps({"valid": True, "cases": len(cases)}, ensure_ascii=False))
        return 0

    report = run(args.cases)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.md_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_report.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "total": report["total"],
                "passed": report["passed"],
                "pass_rate": report["pass_rate"],
                "route_accuracy": report["route_accuracy"],
                "tool_accuracy": report["tool_accuracy"],
                "security": f"{report['security_passed']}/{report['security_total']}",
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
