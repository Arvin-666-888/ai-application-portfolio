import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

TEST_ROOT = Path(tempfile.mkdtemp(prefix="agent_demo_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'meta.db').as_posix()}"
os.environ["SAMPLE_DB_PATH"] = (TEST_ROOT / "sample_data" / "sample.db").as_posix()
os.environ["CHART_DIR"] = (TEST_ROOT / "charts").as_posix()
os.environ["API_KEY"] = ""
os.environ["DEBUG"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key-with-more-than-32-bytes"


@pytest.fixture()
def client():
    from app.database import Base, engine
    from app.main import app
    from app.utils.db_connector import ConnectionManager

    ConnectionManager._connectors.clear()
    Base.metadata.drop_all(bind=engine)

    with TestClient(app) as test_client:
        yield test_client

    ConnectionManager._connectors.clear()
    Base.metadata.drop_all(bind=engine)


def auth_headers(client: TestClient, username: str = "demo_user") -> dict[str, str]:
    password = "password123"
    register_response = client.post(
        "/api/auth/register",
        json={"username": username, "password": password},
    )
    assert register_response.status_code in {200, 400}

    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def create_sample_datasource(client: TestClient, headers: dict[str, str]) -> int:
    from app.config import settings

    response = client.post(
        "/api/datasources",
        headers=headers,
        json={
            "name": "内置财务样例库",
            "db_type": "sqlite",
            "connection_string": f"sqlite:///{Path(settings.SAMPLE_DB_PATH).as_posix()}",
        },
    )
    assert response.status_code == 200
    return response.json()["id"]
