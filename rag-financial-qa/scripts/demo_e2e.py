import argparse
import json
import time
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = [
    PROJECT_ROOT / "evals" / "fixtures" / "ecommerce_product_manual.txt",
    PROJECT_ROOT / "evals" / "fixtures" / "ecommerce_customs_compliance.txt",
    PROJECT_ROOT / "evals" / "fixtures" / "ecommerce_logistics_records.txt",
]


class DemoClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)
        self.token = ""

    def close(self):
        self.client.close()

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers = {**self._headers(), **headers}
        response = self.client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        return response

    def health(self) -> dict:
        return self.request("GET", "/health").json()

    def register_or_continue(self, username: str, password: str) -> None:
        response = self.client.post(
            f"{self.base_url}/api/auth/register",
            json={"username": username, "password": password},
            timeout=30.0,
        )
        if response.status_code not in (200, 400):
            response.raise_for_status()

    def login(self, username: str, password: str) -> None:
        data = self.request(
            "POST",
            "/api/auth/login",
            json={"username": username, "password": password},
        ).json()
        self.token = data["access_token"]

    def create_knowledge_base(self, name: str) -> int:
        data = self.request(
            "POST",
            "/api/knowledge-bases",
            json={"name": name, "description": "跨境电商商品、关税合规与物流事实 E2E 演示知识库"},
        ).json()
        return int(data["id"])

    def upload_document(self, kb_id: int, path: Path) -> dict:
        with path.open("rb") as file:
            files = {"file": (path.name, file, "text/plain")}
            return self.request("POST", f"/api/documents/upload?kb_id={kb_id}", files=files).json()

    def list_documents(self, kb_id: int) -> list[dict]:
        return self.request("GET", f"/api/documents?kb_id={kb_id}").json()

    def wait_until_ready(self, kb_id: int, expected_count: int, timeout_seconds: int) -> list[dict]:
        deadline = time.time() + timeout_seconds
        last_docs: list[dict] = []

        while time.time() < deadline:
            last_docs = self.list_documents(kb_id)
            finished = [doc for doc in last_docs if doc["status"] in ("ready", "failed")]
            if len(finished) >= expected_count:
                failed = [doc for doc in finished if doc["status"] == "failed"]
                if failed:
                    details = "; ".join(
                        f"{doc['filename']}: {doc.get('error_message') or 'unknown error'}"
                        for doc in failed
                    )
                    raise RuntimeError(f"Document processing failed: {details}")
                return finished
            time.sleep(1)

        raise TimeoutError(f"Documents were not ready after {timeout_seconds}s. Last state: {last_docs}")

    def create_conversation(self, kb_id: int) -> int:
        data = self.request(
            "POST",
            "/api/chat/conversations",
            json={"kb_id": kb_id, "title": "跨境电商商品事实 E2E 演示"},
        ).json()
        return int(data["id"])

    def ask(self, conversation_id: int, question: str) -> dict:
        return self.request(
            "POST",
            f"/api/chat/{conversation_id}",
            json={"question": question},
        ).json()


def assert_source_has_snippet(response: dict, expected_document: str) -> None:
    sources = response.get("sources", [])
    if not sources:
        raise AssertionError(f"Expected sources, got empty response: {response}")

    for source in sources:
        if source.get("document") == expected_document and source.get("snippet"):
            return

    raise AssertionError(f"Expected source {expected_document} with snippet, got: {sources}")


def assert_verified_price(response: dict, expected_document: str) -> None:
    assert_source_has_snippet(response, expected_document)
    if response.get("answer_status") != "verified":
        raise AssertionError(f"Expected verified answer_status, got: {response}")
    facts = (response.get("structured_answer") or {}).get("facts") or []
    if len(facts) != 1:
        raise AssertionError(f"Expected exactly one structured fact, got: {facts}")
    fact = facts[0]
    if not (
        fact.get("fact_type") == "price"
        and fact.get("value_text") == "79.90"
        and fact.get("currency") == "USD"
        and fact.get("sku") == "SKU-A100"
        and fact.get("citation_ids")
    ):
        raise AssertionError(f"Unexpected structured price contract: {fact}")
    known_citations = {source.get("citation_id") for source in response.get("sources", [])}
    if not set(fact["citation_ids"]) <= known_citations:
        raise AssertionError(f"Structured fact cites unknown sources: {response}")
    verification = response.get("verification") or {}
    if not verification.get("passed") or not set(fact["citation_ids"]) <= set(verification.get("verified_citation_ids") or []):
        raise AssertionError(f"Expected verified citations, got: {verification}")


def assert_refusal(response: dict) -> None:
    answer = response.get("answer", "")
    if "无法回答" not in answer and "只回答商品价格" not in answer and "一次只查询一类商品事实" not in answer:
        raise AssertionError(f"Expected refusal answer, got: {answer}")
    if response.get("sources"):
        raise AssertionError(f"Refusal answer should not include sources, got: {response['sources']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an end-to-end demo check against a running RAG API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL.")
    parser.add_argument("--username", default=f"demo_user_{int(time.time())}", help="Demo username.")
    parser.add_argument("--password", default="demo-password-123", help="Demo password.")
    parser.add_argument("--timeout", type=int, default=60, help="Document processing timeout in seconds.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = DemoClient(args.base_url)

    try:
        print("[1/8] Checking service health...")
        print(json.dumps(client.health(), ensure_ascii=False))

        print("[2/8] Registering and logging in...")
        client.register_or_continue(args.username, args.password)
        client.login(args.username, args.password)

        print("[3/8] Creating knowledge base...")
        kb_id = client.create_knowledge_base("E2E 跨境电商商品事实知识库")
        print(f"kb_id={kb_id}")

        print("[4/8] Uploading fixture documents...")
        for fixture in FIXTURES:
            result = client.upload_document(kb_id, fixture)
            print(f"uploaded {fixture.name}: doc_id={result['id']} status={result['status']}")

        print("[5/8] Waiting for documents to become ready...")
        docs = client.wait_until_ready(kb_id, expected_count=len(FIXTURES), timeout_seconds=args.timeout)
        print(json.dumps(
            [{"id": doc["id"], "filename": doc["filename"], "status": doc["status"], "chunks": doc["chunk_count"]} for doc in docs],
            ensure_ascii=False,
            indent=2,
        ))

        print("[6/8] Creating conversation...")
        conversation_id = client.create_conversation(kb_id)
        print(f"conversation_id={conversation_id}")

        print("[7/8] Asking ecommerce price question and checking sources...")
        answerable = client.ask(conversation_id, "2026-07-15 Amazon 美国市场 SKU-A100 轻量旅行背包的价格是多少？")
        assert_verified_price(answerable, "ecommerce_product_manual.txt")
        print(json.dumps(answerable, ensure_ascii=False, indent=2))

        print("[8/8] Asking unsupported product-spec question and checking refusal...")
        refusal = client.ask(conversation_id, "SKU-A100 的重量是多少？")
        assert_refusal(refusal)
        print(json.dumps(refusal, ensure_ascii=False, indent=2))

        print("E2E demo check passed.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
