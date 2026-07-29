from __future__ import annotations

import logging
import os
import random
from typing import Any

import requests
from locust import HttpUser, between, events, task
from locust.exception import StopUser

from benchmark.config import BenchmarkConfig, build_url, business_response_error

logger = logging.getLogger(__name__)

AGENT_QUESTIONS = (
    "2024 年每月收入趋势如何？",
    "各产品线毛利率是多少？",
    "收入贡献最高的客户是谁？",
    "哪些部门预算执行率最高？",
    "应收账款风险如何？",
)

RAG_QUESTIONS = (
    "本期营业收入是多少？",
    "归母净利润是多少？",
    "经营活动现金流如何变化？",
    "毛利率是多少？",
    "主要风险因素有哪些？",
)


class InitializationError(RuntimeError):
    pass


class BenchmarkUser(HttpUser):
    """One virtual user exercises both real repository APIs with separate JWTs."""

    host = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8001")
    wait_time = between(1, 3)

    config: BenchmarkConfig
    agent_headers: dict[str, str]
    rag_headers: dict[str, str]
    agent_ds_id: int
    rag_conversation_id: int

    def on_start(self) -> None:
        try:
            self.config = BenchmarkConfig.from_env()
            self.agent_headers = {}
            self.rag_headers = {}
            self.agent_ds_id = 0
            self.rag_conversation_id = 0

            if "agent" in self.config.scenarios:
                self._initialize_agent()
            if "rag" in self.config.scenarios:
                self._initialize_rag()
            events.request.fire(
                request_type="INIT",
                name="Initialization",
                response_time=0,
                response_length=0,
                exception=None,
            )
        except (InitializationError, requests.RequestException, ValueError) as exc:
            logger.error("Stopping virtual user because initialization failed: %s", exc)
            events.request.fire(
                request_type="INIT",
                name="Initialization",
                response_time=0,
                response_length=0,
                exception=exc,
            )
            raise StopUser() from exc

    @task(1)
    def agent_chat(self) -> None:
        if "agent" not in self.config.scenarios:
            return

        with self.client.post(
            build_url(self.config.agent_base_url, self.config.agent_path),
            name="agent_chat",
            headers=self.agent_headers,
            json={"question": random.choice(AGENT_QUESTIONS), "ds_id": self.agent_ds_id},
            timeout=self.config.request_timeout,
            catch_response=True,
        ) as response:
            self._validate_business_response(response, "Agent", required_key="answer")

    @task(1)
    def rag_query(self) -> None:
        if "rag" not in self.config.scenarios:
            return

        path = self.config.rag_path.format(conversation_id=self.rag_conversation_id)
        with self.client.post(
            build_url(self.config.rag_base_url, path),
            name="rag_query",
            headers=self.rag_headers,
            json={"question": random.choice(RAG_QUESTIONS)},
            timeout=self.config.request_timeout,
            catch_response=True,
        ) as response:
            self._validate_business_response(response, "RAG", required_key="answer")

    def _initialize_agent(self) -> None:
        session = requests.Session()
        token = self._register_and_login(session, self.config.agent_base_url, "Agent")
        self.agent_headers = {"Authorization": f"Bearer {token}"}

        datasources = self._request_json(
            session,
            "GET",
            self.config.agent_base_url,
            "/api/datasources",
            "Agent datasource list",
            headers=self.agent_headers,
        )
        if not isinstance(datasources, list):
            raise InitializationError("Agent datasource list did not return a JSON array")

        if self.config.agent_ds_id is not None:
            matching = [item for item in datasources if int(item.get("id", 0)) == self.config.agent_ds_id]
            if not matching:
                raise InitializationError(
                    f"AGENT_DS_ID={self.config.agent_ds_id} is not owned by the benchmark user"
                )
            ds_id = self.config.agent_ds_id
        else:
            matching = [item for item in datasources if item.get("name") == "Locust 内置财务样例库"]
            if matching:
                ds_id = int(matching[0]["id"])
            else:
                created = self._request_json(
                    session,
                    "POST",
                    self.config.agent_base_url,
                    "/api/datasources",
                    "Agent datasource creation",
                    headers=self.agent_headers,
                    json={
                        "name": "Locust 内置财务样例库",
                        "db_type": "sqlite",
                        "connection_string": self.config.agent_ds_connection_string,
                    },
                )
                ds_id = int(created["id"])

        schema = self._request_json(
            session,
            "GET",
            self.config.agent_base_url,
            f"/api/datasources/{ds_id}/schema",
            "Agent datasource validation",
            headers=self.agent_headers,
        )
        tables = schema.get("tables", []) if isinstance(schema, dict) else []
        if not tables:
            raise InitializationError(f"Agent datasource {ds_id} has no readable tables")
        self.agent_ds_id = ds_id

    def _initialize_rag(self) -> None:
        session = requests.Session()
        token = self.config.rag_access_token
        if not token:
            token = self._register_and_login(session, self.config.rag_base_url, "RAG")
        self.rag_headers = {"Authorization": f"Bearer {token}"}

        if self.config.rag_conversation_id is None:
            raise InitializationError(
                "RAG_CONVERSATION_ID is required. Upload and index documents once, then create "
                "a conversation before starting the benchmark."
            )

        conversations = self._request_json(
            session,
            "GET",
            self.config.rag_base_url,
            "/api/chat/conversations",
            "RAG conversation list",
            headers=self.rag_headers,
        )
        conversation = next(
            (
                item
                for item in conversations
                if int(item.get("id", 0)) == self.config.rag_conversation_id
            ),
            None,
        )
        if conversation is None:
            raise InitializationError(
                f"RAG_CONVERSATION_ID={self.config.rag_conversation_id} is not owned by the RAG user/token"
            )

        kb_id = int(conversation["kb_id"])
        documents = self._request_json(
            session,
            "GET",
            self.config.rag_base_url,
            f"/api/documents?kb_id={kb_id}",
            "RAG document readiness check",
            headers=self.rag_headers,
        )
        ready = [
            doc
            for doc in documents
            if doc.get("status") == "ready" and int(doc.get("chunk_count", 0)) > 0
        ]
        if not ready:
            failures = [
                f"{doc.get('filename', '?')}: {doc.get('status')} {doc.get('error_message', '')}".strip()
                for doc in documents
            ]
            detail = "; ".join(failures) or "knowledge base contains no documents"
            raise InitializationError(
                "RAG knowledge base has no ready indexed document with chunks: " + detail
            )

        created = self._request_json(
            session,
            "POST",
            self.config.rag_base_url,
            "/api/chat/conversations",
            "RAG benchmark conversation creation",
            headers=self.rag_headers,
            json={"kb_id": kb_id, "title": "Locust benchmark conversation"},
        )
        self.rag_conversation_id = int(created["id"])

    def _register_and_login(
        self, session: requests.Session, base_url: str, service_name: str
    ) -> str:
        credentials = {"username": self.config.username, "password": self.config.password}
        register = session.post(
            build_url(base_url, "/api/auth/register"),
            json=credentials,
            timeout=self.config.request_timeout,
        )
        if register.status_code not in {200, 400}:
            raise InitializationError(
                f"{service_name} registration failed with HTTP {register.status_code}: "
                f"{register.text[:300]}"
            )

        login = session.post(
            build_url(base_url, "/api/auth/login"),
            json=credentials,
            timeout=self.config.request_timeout,
        )
        if login.status_code != 200:
            raise InitializationError(
                f"{service_name} login failed with HTTP {login.status_code}: {login.text[:300]}"
            )
        token = login.json().get("access_token")
        if not token:
            raise InitializationError(f"{service_name} login response did not contain access_token")
        return str(token)

    def _request_json(
        self,
        session: requests.Session,
        method: str,
        base_url: str,
        path: str,
        operation: str,
        **kwargs: Any,
    ) -> Any:
        response = session.request(
            method,
            build_url(base_url, path),
            timeout=self.config.request_timeout,
            **kwargs,
        )
        if not response.ok:
            raise InitializationError(
                f"{operation} failed with HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise InitializationError(f"{operation} did not return JSON") from exc

    @staticmethod
    def _validate_business_response(response: Any, service: str, required_key: str) -> None:
        if not response.ok:
            response.failure(f"{service} HTTP {response.status_code}: {response.text[:300]}")
            return
        try:
            body = response.json()
        except ValueError:
            response.failure(f"{service} returned non-JSON response")
            return
        error = business_response_error(body, service, required_key)
        if error:
            response.failure(error)
