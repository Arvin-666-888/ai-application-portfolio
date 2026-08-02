# Locust 双服务压测

## 数据流架构

```text
[Locust 10 Users]
    │
    ├─ on_start 初始化（初始化失败记录为 `benchmark_init` 失败，不计入业务延迟）
    │      ├─ Agent 注册/登录 ──► 获取 Agent JWT
    │      ├─ Agent 列出/创建 datasource ──► 校验 schema 非空
    │      ├─ RAG 登录或使用 RAG_ACCESS_TOKEN ──► 获取 RAG JWT
    │      └─ 检查 conversation ──► 确认知识库至少有一个 ready 且 chunk_count > 0 的文档
    │
    ├─ POST agent_chat（固定统计名）
    │      └─ http://127.0.0.1:8001/api/analysis/ask
    │
    └─ POST rag_query（固定统计名）
           └─ http://127.0.0.1:8000/api/chat/{conversation_id}
                       │
                       ▼
          [Locust CSV/HTML] ──► [generate_report.py] ──► [benchmark_result.md]
```

两个服务使用不同 Base URL。`locustfile.py` 向 `self.client.post()` 传入绝对 URL，同时使用固定 `name`，因此动态 conversation ID 不会拆散统计。

## 真实接口映射

| 用户原始路径 | 仓库真实路径 | 请求 JSON | 前置条件 |
|---|---|---|---|
| `POST /agent/chat` | `POST /api/analysis/ask` | `{"question": "...", "ds_id": 1}` | Agent JWT；当前用户拥有且可读取的 datasource |
| `POST /rag/query` | `POST /api/chat/{conversation_id}` | `{"question": "..."}` | RAG JWT；当前用户拥有的 conversation；其知识库已完成文档索引 |

仓库没有 `/agent/chat` 或 `/rag/query` 兼容路由，本任务不修改业务 Router。默认路径采用真实路由，也可用 `AGENT_PATH` 和 `RAG_PATH` 覆盖。

- Agent 健康检查：`GET http://127.0.0.1:8001/health`（根 Compose 将容器 8000 映射到宿主 8001）。
- RAG 健康检查：`GET http://127.0.0.1:8000/health`。
- 两个服务均通过 `POST /api/auth/register`、`POST /api/auth/login` 获取各自 JWT；即使用户名相同，Token 也不能跨服务复用。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_BASE_URL` | `http://127.0.0.1:8001` | Agent 服务完整 Base URL |
| `RAG_BASE_URL` | `http://127.0.0.1:8000` | RAG 服务完整 Base URL |
| `AGENT_PATH` | `/api/analysis/ask` | Agent 真实路径 |
| `RAG_PATH` | `/api/chat/{conversation_id}` | RAG 路径，必须保留占位符 |
| `BENCHMARK_USERNAME` | `benchmark_user` | 明确用于本地 benchmark 的测试用户名 |
| `BENCHMARK_PASSWORD` | `benchmark-password-123` | 明确用于本地 benchmark 的测试密码；真实环境应覆盖 |
| `AGENT_DS_ID` | 空 | 已属于 benchmark 用户的数据源 ID；为空时复用/创建样例数据源 |
| `AGENT_DS_CONNECTION_STRING` | `sqlite:////app/data/sample.db` | Agent Compose 容器内样例库路径；本地进程运行时需显式改为本机路径 |
| `RAG_ACCESS_TOKEN` | 空 | 可选 RAG JWT；为空时注册/登录 benchmark 用户 |
| `RAG_CONVERSATION_ID` | 空（必填） | 已绑定到完成索引知识库的对话 ID |
| `REQUEST_TIMEOUT` | `60` | 单请求超时秒数 |
| `BENCHMARK_USE_MOCK` | `true` | 仅用于结果元数据标识，不会模拟 HTTP 服务或伪造响应 |
| `BENCHMARK_SCENARIOS` | `agent,rag` | 可设为 `agent`、`rag` 或两者，便于隔离诊断 |

初始化使用独立 `requests.Session`，不进入 `agent_chat`/`rag_query` 延迟指标。注册已存在（HTTP 400）可继续登录；任何登录、数据源、conversation 或索引检查失败都会记录 `benchmark_init` 失败并停止该虚拟用户，避免静默减少有效并发人数。

## RAG 压测前置准备

不要在每个虚拟用户中上传文档。先按 RAG 项目的真实流程准备一次：

1. 注册并登录 RAG 服务。
2. `POST /api/knowledge-bases` 创建知识库。
3. `POST /api/documents/upload?kb_id={id}` 上传 `rag-financial-qa/evals/fixtures/` 文档。
4. 轮询 `GET /api/documents?kb_id={id}`，确认 `status=ready` 且 `chunk_count>0`。
5. `POST /api/chat/conversations` 创建一个只用于确认知识库归属和 readiness 的种子 conversation。
6. 将返回 ID 写入 `RAG_CONVERSATION_ID`；若 conversation 属于不同用户，同时提供对应 `RAG_ACCESS_TOKEN` 或匹配的用户名密码。
7. 每个 Locust 虚拟用户启动时会基于该 conversation 的 `kb_id` 创建自己的独立 conversation，避免并发用户共享历史。

可复用仓库脚本完成上述流程并获得 ID：

```bash
python rag-financial-qa/scripts/demo_e2e.py --base-url http://127.0.0.1:8000
```

该脚本还会发起问答。正式压测前应确认输出中的文档均为 `ready`。

## 安装

在仓库根目录运行：

```bash
python -m pip install -r benchmark/requirements.txt
```

当前环境实际验证版本为 `locust==2.46.1`，已精确锁定。

## 标准运行命令（Windows Git Bash）

先设置至少 `RAG_CONVERSATION_ID`。多行命令：

```bash
RAG_CONVERSATION_ID=1 python -m locust \
  -f benchmark/locustfile.py \
  --headless \
  -u 10 \
  -r 2 \
  -t 2m \
  --csv benchmark/results/benchmark \
  --html benchmark/results/benchmark_report.html
```

单行版本：

```bash
RAG_CONVERSATION_ID=1 python -m locust -f benchmark/locustfile.py --headless -u 10 -r 2 -t 2m --csv benchmark/results/benchmark --html benchmark/results/benchmark_report.html
```

建议先执行 1 用户、15 秒 smoke：

```bash
RAG_CONVERSATION_ID=1 python -m locust -f benchmark/locustfile.py --headless -u 1 -r 1 -t 15s --csv benchmark/results/smoke
```

如果只排查 Agent，可暂时设置 `BENCHMARK_SCENARIOS=agent`；这不能替代双接口正式验收。

## 生成结果报告

正式运行完成后，创建 metadata JSON，例如：

```json
{
  "status": "通过",
  "run_time": "2m",
  "users": 10,
  "initialized_users_verified": true,
  "spawn_rate": 2,
  "agent_base_url": "http://127.0.0.1:8001",
  "rag_base_url": "http://127.0.0.1:8000",
  "mode": "mock",
  "dataset": "内置财务样例库；RAG conversation 1",
  "local_docker": "是"
}
```

然后运行：

```bash
python benchmark/generate_report.py --stats benchmark/results/benchmark_stats.csv --failures benchmark/results/benchmark_failures.csv --metadata benchmark/results/metadata.json --output benchmark/benchmark_result.md
```

生成器读取 Locust 2.46.1 的 `95%`、`99%` 字段，并兼容 Windows PowerShell 5 `Set-Content -Encoding UTF8` 生成的 UTF-8 BOM metadata 文件。只有 Agent、RAG、Aggregated 请求数一致、零失败、10 用户、2 分钟、初始化人数已验证且 failures CSV 为空时才允许 `status=通过`；报告同时记录输入 CSV 的 SHA-256。

## 验证

```bash
python -m compileall -q benchmark
```

```bash
python -m pytest benchmark -q
```

```bash
python -m locust -f benchmark/locustfile.py --help
```

## 真实性边界

- `BENCHMARK_USE_MOCK=true` 表示业务服务运行在无模型 API Key 的 mock 模式；它不启动 mock server，也不把失败改成成功。
- 只有双接口返回不含模型失败或 mock 标记的非空 `answer`、完成 10 用户 2 分钟、全部目标用户初始化已机器验证、stats 聚合一致且 failures CSV 为空，才可将结果标为“通过”。
- 空知识库、未完成索引、服务不可达、全量 401/404/500 或只跑短 smoke 都必须标为“部分通过”或“阻塞”。
- 本机结果受 CPU、内存、Docker、数据库连接池、知识库规模和上游模型延迟影响，不能直接等同生产容量。
