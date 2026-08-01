import argparse
import json
import logging
import os
import re
import sys
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

EVAL_ROOT = Path(tempfile.mkdtemp(prefix="agent_eval_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(EVAL_ROOT / 'meta.db').as_posix()}"
os.environ["SAMPLE_DB_PATH"] = (EVAL_ROOT / "sample_data" / "sample.db").as_posix()
os.environ["CHART_DIR"] = (EVAL_ROOT / "charts").as_posix()
os.environ["DEBUG"] = "false"
os.environ["SECRET_KEY"] = "agent-eval-secret-key-with-more-than-32-bytes"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.WARNING)
logging.getLogger("passlib").setLevel(logging.ERROR)
warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline evals for the data-analysis Agent demo.")
    parser.add_argument("--questions", default="evals/agent_questions.jsonl", help="Path to JSONL eval cases.")
    parser.add_argument("--real-llm", action="store_true", help="Use API_KEY from .env/environment instead of mock mode.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary only.")
    return parser.parse_args()


def load_cases(path: str) -> list[dict[str, Any]]:
    cases = []
    with open(PROJECT_ROOT / path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            if not case.get("id") or not case.get("question"):
                raise ValueError(f"Missing id/question at {path}:{line_no}")
            case.setdefault("expected_tools", [])
            case.setdefault("expected_sql_contains", [])
            case.setdefault("expected_scope_fields", [])
            case.setdefault("expected_min_rows", 0)
            case.setdefault("should_block", False)
            if not case.get("sql_to_validate") and not case.get("expected_answer_contains"):
                raise ValueError(
                    f"Agent case requires non-empty expected_answer_contains at {path}:{line_no}"
                )
            cases.append(case)
    return cases


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value).lower())


def contains_all(text: str, expected_items: list[str]) -> bool:
    normalized = normalize_text(text)
    return all(normalize_text(item) in normalized for item in expected_items)


def expected_tools_match(actual_tools: list[str], expected_tools: list[str]) -> bool | None:
    if not expected_tools:
        return None
    cursor = 0
    for tool in actual_tools:
        if cursor < len(expected_tools) and tool == expected_tools[cursor]:
            cursor += 1
    return cursor == len(expected_tools)


def mark(value: bool | None) -> str:
    if value is None:
        return "N/A"
    return "PASS" if value else "FAIL"


def average_bool(values: list[bool | None]) -> float | None:
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return sum(1 for value in valid if value) / len(valid)


def pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def setup_client(use_real_llm: bool):
    if not use_real_llm:
        os.environ["API_KEY"] = ""

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.database import Base, engine
    from app.main import app
    from app.utils.db_connector import ConnectionManager

    ConnectionManager._connectors.clear()
    Base.metadata.drop_all(bind=engine)

    client = TestClient(app)
    client.__enter__()

    username = f"agent_eval_{uuid.uuid4().hex[:8]}"
    password = "password123"
    shop_id = "amazon-us"
    credentials = {"shop_id": shop_id, "username": username, "password": password}
    client.post("/api/auth/register", json=credentials).raise_for_status()
    login_response = client.post("/api/auth/login", json=credentials)
    login_response.raise_for_status()
    headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}
    ds_response = client.post(
        "/api/datasources",
        headers=headers,
        json={
            "name": "内置跨境电商样例库",
            "db_type": "sqlite",
            "connection_string": f"sqlite:///{Path(settings.SAMPLE_DB_PATH).as_posix()}",
        },
    )
    ds_response.raise_for_status()
    return client, headers, ds_response.json()["id"]


def evaluate_sql_guardrail(case: dict[str, Any]) -> dict[str, Any]:
    from app.utils.sql_safety import validate_sql

    is_valid, message = validate_sql(case["sql_to_validate"])
    safety_pass = (not is_valid) if case.get("should_block") else is_valid
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "mode": "sql_guardrail",
        "tools": [],
        "rows": 0,
        "sql": case["sql_to_validate"],
        "tool_match": None,
        "sql_match": None,
        "row_match": None,
        "answer_match": None,
        "scope_match": None,
        "safety_pass": safety_pass,
        "passed": safety_pass,
        "message": message,
    }


def evaluate_agent_case(client, headers: dict[str, str], ds_id: int, case: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/analysis/ask",
        headers=headers,
        json={"ds_id": ds_id, "question": case["question"]},
    )
    response.raise_for_status()
    body = response.json()

    actual_tools = [item.get("tool", "") for item in body.get("tool_trace", [])]
    tool_match = expected_tools_match(actual_tools, case.get("expected_tools", []))
    sql_match = contains_all(body.get("sql_query", ""), case.get("expected_sql_contains", []))
    row_match = len(body.get("data", [])) >= int(case.get("expected_min_rows", 0))

    answer = body.get("answer", "")
    data = body.get("data", [])
    answer_match = contains_all(answer, case["expected_answer_contains"])
    expected_scope_fields = case.get("expected_scope_fields", [])
    time_fields = {
        "report_date", "order_date", "snapshot_date", "observed_at",
        "period_start", "period_end",
    }
    dimension_values = {
        field: sorted({
            str(row[field]) for row in data if row.get(field) not in (None, "")
        })
        for field in expected_scope_fields
        if field not in time_fields
    }
    time_values = sorted({
        str(row[field])
        for row in data
        for field in expected_scope_fields
        if field in time_fields and row.get(field) not in (None, "")
    })
    scope_match = (
        all(values and all(value in answer for value in values) for values in dimension_values.values())
        and (not any(field in time_fields for field in expected_scope_fields)
             or bool(time_values) and time_values[0] in answer and time_values[-1] in answer)
        if expected_scope_fields else None
    )

    checks = [
        value for value in (tool_match, sql_match, row_match, answer_match, scope_match)
        if value is not None
    ]
    passed = all(checks) if checks else True
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "mode": "agent",
        "tools": actual_tools,
        "rows": len(body.get("data", [])),
        "sql": body.get("sql_query", ""),
        "tool_match": tool_match,
        "sql_match": sql_match,
        "row_match": row_match,
        "answer_match": answer_match,
        "scope_match": scope_match,
        "safety_pass": None,
        "passed": passed,
        "message": answer[:160],
    }


def print_case_result(result: dict[str, Any]) -> None:
    print("\n" + "=" * 80)
    print(f"[{result['id']}] {result['question']}")
    print(f"category: {result['category']} mode: {result['mode']}")
    print(f"tools: {result['tools']}")
    print(f"rows: {result['rows']}")
    print(f"tool_match: {mark(result['tool_match'])}")
    print(f"sql_match: {mark(result['sql_match'])}")
    print(f"row_match: {mark(result['row_match'])}")
    print(f"answer_match: {mark(result['answer_match'])}")
    print(f"scope_match: {mark(result['scope_match'])}")
    print(f"safety_pass: {mark(result['safety_pass'])}")
    print(f"passed: {mark(result['passed'])}")
    if result["sql"]:
        print(f"sql: {result['sql'][:240]}")
    if result["message"]:
        print(f"message: {result['message']}")


def build_summary(results: list[dict[str, Any]], real_llm: bool) -> dict[str, Any]:
    return {
        "mode": "real_llm" if real_llm else "mock",
        "total_cases": len(results),
        "passed_cases": sum(1 for result in results if result["passed"]),
        "pass_rate": average_bool([result["passed"] for result in results]),
        "tool_match_rate": average_bool([result["tool_match"] for result in results]),
        "sql_match_rate": average_bool([result["sql_match"] for result in results]),
        "row_match_rate": average_bool([result["row_match"] for result in results]),
        "answer_match_rate": average_bool([result["answer_match"] for result in results]),
        "scope_match_rate": average_bool([result["scope_match"] for result in results]),
        "safety_pass_rate": average_bool([result["safety_pass"] for result in results]),
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 80)
    print("Summary")
    print(f"mode: {summary['mode']}")
    print(f"total_cases: {summary['total_cases']}")
    print(f"passed_cases: {summary['passed_cases']}")
    print(f"pass_rate: {pct(summary['pass_rate'])}")
    print(f"tool_match_rate: {pct(summary['tool_match_rate'])}")
    print(f"sql_match_rate: {pct(summary['sql_match_rate'])}")
    print(f"row_match_rate: {pct(summary['row_match_rate'])}")
    print(f"answer_match_rate: {pct(summary['answer_match_rate'])}")
    print(f"scope_match_rate: {pct(summary['scope_match_rate'])}")
    print(f"safety_pass_rate: {pct(summary['safety_pass_rate'])}")


def main() -> int:
    args = parse_args()
    cases = load_cases(args.questions)

    client = None
    results = []
    try:
        client, headers, ds_id = setup_client(args.real_llm)
        for case in cases:
            if case.get("sql_to_validate"):
                result = evaluate_sql_guardrail(case)
            else:
                result = evaluate_agent_case(client, headers, ds_id, case)
            results.append(result)
            if not args.json:
                print_case_result(result)
    finally:
        if client is not None:
            client.__exit__(None, None, None)

    summary = build_summary(results, args.real_llm)
    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    else:
        print_summary(summary)
    return 0 if summary["passed_cases"] == summary["total_cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
