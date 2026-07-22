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
- [x] Push to GitHub and observe the remote Actions run.
- [x] Create the `v1.0.0` Git tag after the remote workflow passes.

Release evidence:

- V1 commit: `9b8d05ceeb3f785b6ffc8f362e1205bf188dc840`.
- Release-branch CI: [run 29938088403](https://github.com/Arvin-666-888/ai-application-portfolio/actions/runs/29938088403) — passed.
- Main-branch CI: [run 29938260727](https://github.com/Arvin-666-888/ai-application-portfolio/actions/runs/29938260727) — passed.
- Annotated tag: [`v1.0.0`](https://github.com/Arvin-666-888/ai-application-portfolio/releases/tag/v1.0.0).
