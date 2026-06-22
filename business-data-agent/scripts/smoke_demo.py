import argparse
import json
import logging
import os
import sys
import warnings
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def configure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def configure_environment(use_real_llm: bool):
    if not use_real_llm:
        os.environ["API_KEY"] = ""
    os.environ.setdefault("DEBUG", "false")


def main():
    parser = argparse.ArgumentParser(description="Run a local end-to-end demo without starting uvicorn.")
    parser.add_argument("--real-llm", action="store_true", help="Use API_KEY from .env/environment instead of mock mode.")
    args = parser.parse_args()

    configure_stdout()
    configure_environment(args.real_llm)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("passlib").setLevel(logging.ERROR)
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
    )

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    username = f"demo_{uuid.uuid4().hex[:8]}"
    password = "password123"

    with TestClient(app) as client:
        register_response = client.post(
            "/api/auth/register",
            json={"username": username, "password": password},
        )
        register_response.raise_for_status()

        login_response = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        login_response.raise_for_status()
        token = login_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        ds_response = client.post(
            "/api/datasources",
            headers=headers,
            json={
                "name": "内置财务样例库",
                "db_type": "sqlite",
                "connection_string": f"sqlite:///{Path(settings.SAMPLE_DB_PATH).as_posix()}",
            },
        )
        ds_response.raise_for_status()
        ds_id = ds_response.json()["id"]

        ask_response = client.post(
            "/api/analysis/ask",
            headers=headers,
            json={"ds_id": ds_id, "question": "2024 年每月收入趋势如何？"},
        )
        ask_response.raise_for_status()
        body = ask_response.json()

        report_response = client.get(
            f"/api/analysis/export/report/{body['id']}",
            headers=headers,
        )
        report_response.raise_for_status()

    summary = {
        "mode": "real_llm" if args.real_llm else "mock",
        "record_id": body["id"],
        "answer_preview": body["answer"][:160],
        "sql_contains": "revenue_records" if "revenue_records" in body["sql_query"] else "unknown",
        "rows": len(body["data"]),
        "tools": [item["tool"] for item in body["tool_trace"]],
        "report_bytes": len(report_response.content),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
