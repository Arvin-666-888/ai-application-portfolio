# RAG 演示与验证 Runbook

这份文档记录 RAG 项目的本地验证顺序，用于确认文档入库、检索问答、来源返回和拒答逻辑可以稳定运行。

## 1. 基础检查

```powershell
$env:SECRET_KEY="<至少 32 字符的随机测试密钥>"
pytest
python evals/run_eval.py --validate-only
```

首次安装环境时先执行 `pip install -r requirements.txt`。非 DEBUG 启动必须显式配置随机 `SECRET_KEY`；DEBUG + `ALLOW_INSECURE_DEMO_MODE=true` 会生成仅当前进程有效的临时随机密钥，不使用仓库中的固定 placeholder。

期望结果：

- 以本轮 fresh `pytest` 终端输出为准，不使用文档中的历史固定测试数代替当前验证。
- `evals/run_eval.py --validate-only` 输出本轮评测集结构与实际 case 数；不要仅凭历史 24 条记录判断当前文件。

## 2. 迁移并启动服务与 Worker

```powershell
python scripts/migrate_router_v2.py --apply
uvicorn app.main:app --reload --port 8000
```

API 与普通文档 worker 必须同时存活。上传接口只负责持久入队；如果未启动 `document_worker`，文档会停留在 `queued`，不会变为 `ready`。如使用 Docker Compose，`api` 与 `document-worker` 服务会一起启动。

```powershell
python -m app.workers.document_worker
```

如需 L3 在线增强，再在已锁定的独立 PaddleOCR venv 中启动：

```powershell
python -m app.workers.paddle_worker
```

访问：

```text
http://127.0.0.1:8000/docs
```

## 3. 最小端到端验证

```powershell
python scripts/demo_e2e.py --base-url http://127.0.0.1:8000
```

脚本会依次完成：

1. 注册和登录。
2. 创建知识库。
3. 上传样例金融文档。
4. 等待文档状态变为 `ready`。
5. 创建对话。
6. 提问资料内问题，检查 answer 和 sources。
7. 提问股价预测类问题，检查拒答逻辑。

## 4. 推荐手工问题

- `公司 2024 年营业收入是多少？`
- `2024 年毛利率是多少，为什么提升？`
- `云资源价格上升会带来什么风险？`
- `竞争对手A公司2024年的营业收入是多少？`
- `请预测公司明年股价会涨到多少？`

## 5. LangChain 对照验证

```powershell
pip install -r requirements-langchain.txt
python examples/langchain_rag_demo.py --mock --question "2024年公司营业收入是多少？"
python examples/langchain_rag_demo.py --mock --question "竞争对手A公司收入是多少？"
```

对照 demo 应返回 `answer`、`sources`、`snippet` 和 `relevance`，并能展示资料不足时的拒答路径。

## 6. V3 可信答案验证

默认仍为 legacy。需要在开发环境验证 V3 工程路径时，显式设置：

```powershell
$env:RAG_ANSWER_PROFILE="verified_v3"
```

真实模型必须返回结构化 JSON；数值、单位、年度、公司、指标、口径或 citation 任一不匹配都会 fail closed。同步响应保留 `answer/sources`，并增加 `answer_status/structured_answer/verification/run`；SSE 顺序为 `meta -> content -> sources -> result -> [DONE]`。可用 `trace_id` 查询：

```text
GET /api/chat/runs/{trace_id}
```

健康与就绪检查：

```text
GET /health
GET /ready
```

## 7. 当前边界

- mock mode 只能验证流程，不代表真实语义质量；自动测试也只证明工程合同和回归行为。
- legacy sources 仍只是检索片段来源；只有 `verified_v3` 且 `verification.passed=true` 的数值 fact 才经过确定性一致性校验。
- PDF 三层 Router 与 SQLite 持久 worker/queue 已实现；SQLite 仅适合单机低并发原型，L3 Paddle worker 仍要求独立 GPU/模型环境。
- historical/disclosed holdout 的 Gate B 只有 AI 盲标草稿上的 provisional `12/24`，没有独立人工 Ground Truth attestation，不能 finalize 或据此切换默认检索配置。
- Gate C 在同一披露上下文上真实执行但失败：`verified_v3=0/24 accepted`，其中 20/24 在模型调用前因 `no_fact_binding` 拒绝；这不是质量通过证据。
- 根因修复后的新 sealed holdout 尚未执行。默认继续为 `PDF_PADDLE_ARTIFACT_ENABLED=false`、`RETRIEVAL_PROFILE=legacy`、`RAG_ANSWER_PROFILE=legacy`、`TOP_K=3`。
- V3 加强单用户所有权与运行审计，没有实现多租户、RBAC、SSO 或不可篡改合规审计。
