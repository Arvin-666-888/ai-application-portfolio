# V1.0 Release Checklist

- [x] FastAPI `version=1.0.0` and `/health` version output.
- [x] Stable `POST /api/v1/chat` endpoint.
- [x] Four LangGraph routes: catalog, order, aftersales, unsupported.
- [x] JWT identity and order ownership checks.
- [x] Sensitive aftersales actions remain proposals only.
- [x] Tool Trace and redacted per-user audit logs.
- [x] Deterministic seed data and smoke demo.
- [x] 30-case JSONL evaluation with security cases.
- [x] Exact dependency versions and Docker Compose.
- [x] Local pytest, evaluation, smoke, dependency and HTTP checks.
- [ ] Push to GitHub and observe the remote Actions run.
- [ ] Create the `v1.0.0` Git tag after the remote workflow passes.

The unchecked items require an explicit Git push/release action and are not claimed as completed locally.
