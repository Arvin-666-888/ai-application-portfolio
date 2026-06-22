# 面试前演示验收清单

这份清单用于面试前自检。目标不是“感觉能跑”，而是确认演示链路真的稳定。

## 1. 环境检查

- Python 版本建议 3.10+。
- 已安装依赖：

```bash
cd demo
pip install -r requirements-dev.txt
```

- 面试前总验收：

```bash
python scripts/pre_interview_check.py
```

如果当前只是做代码和材料检查，还没有安装运行依赖，可以先用：

```bash
python scripts/pre_interview_check.py --allow-missing-deps
```

但正式演示前必须安装依赖，并让 `pre_interview_check.py` 不带 `--allow-missing-deps` 通过。

- `.env` 已从 `.env.example` 复制。
- 如果要展示真实模型效果，`.env` 里已填写 `API_KEY`、`BASE_URL`、`MODEL`、`EMBEDDING_MODEL`。
- 如果只展示接口链路，可以留空 `API_KEY`，系统会使用 mock mode。

## 2. 启动服务

本地启动：

```bash
cd demo
uvicorn app.main:app --reload --port 8000
```

或 Docker 启动：

```bash
cd demo
docker compose up --build
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

期望结果：

```json
{"status":"ok"}
```

## 3. 一键端到端验收

服务启动后，在另一个终端运行：

```bash
cd demo
python scripts/demo_e2e.py --base-url http://127.0.0.1:8000
```

脚本会自动完成：

- 注册/登录。
- 创建知识库。
- 上传 `finance_summary_2024.txt` 和 `risk_notice.txt`。
- 等待文档状态变为 `ready`。
- 创建对话。
- 提问资料内问题，并检查 sources 里有 snippet。
- 提问股价预测问题，并检查系统拒答。

通过标准：

```text
E2E demo check passed.
```

## 4. Swagger 手工演示

访问：

```text
http://127.0.0.1:8000/docs
```

演示顺序：

1. `POST /api/auth/register`
2. `POST /api/auth/login`
3. Swagger 右上角 Authorize，填 `Bearer <token>`
4. `POST /api/knowledge-bases`
5. `POST /api/documents/upload`
6. `GET /api/documents`
7. `POST /api/chat/conversations`
8. `POST /api/chat/{conversation_id}`

资料内问题：

```text
公司2024年营业收入是多少？
```

你要展示：

- `answer` 有回答。
- `sources[0].document` 指向 `finance_summary_2024.txt`。
- `sources[0].snippet` 包含原文片段。
- `sources[0].relevance` 是检索相关度，不是正确率。

拒答问题：

```text
请预测公司明年股价会涨到多少？
```

你要展示：

- 系统明确拒答。
- 不返回 sources。
- 解释这是应用层金融护栏，不完全依赖 Prompt。

## 5. 自动评测

上传 `evals/fixtures` 里的两个文档到同一个知识库后运行：

```bash
python evals/run_eval.py --kb-id <你的知识库ID> --top-k 3
```

只测检索：

```bash
python evals/run_eval.py --kb-id <你的知识库ID> --top-k 3 --retrieval-only
```

面试时可解释的指标：

- `retrieval_hit_rate@3`：检索是否找到了正确资料。
- `source_support_rate`：返回的 sources 是否能支撑答案。
- `refusal_accuracy`：资料外问题是否拒答。
- `answer_keyword_match_rate`：答案是否包含关键事实。

## 6. 常见故障

### 文档一直 processing

检查服务端日志。常见原因：

- 依赖没装完整。
- 文件路径权限问题。
- Embedding API 请求失败。

现在 `GET /api/documents` 会返回 `error_message`，如果状态是 `failed`，先看这个字段。

### sources 为空

可能原因：

- 文档还没 ready。
- 问题和文档不相关。
- `MIN_RELEVANCE_SCORE` 过高。
- mock embedding 下语义质量不稳定。

### 真实模型效果不好

优先排查：

- 文档是否正确切分。
- Top-K 是否太小。
- 关键词重排权重是否合适。
- Prompt 是否要求模型只基于资料回答。
- 评测集是否覆盖了当前问题类型。
