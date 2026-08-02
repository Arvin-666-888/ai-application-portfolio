# Cross-Border Ecommerce AI Application Portfolio

面向跨境电商场景的 AI 应用工程项目集，覆盖 **多 Agent 客服、经营数据分析 Agent、商品文档 RAG** 三条完整后端链路。

项目统一使用 Python、FastAPI、SQLAlchemy、Pydantic、SQLite 和 pytest；在需要状态编排时使用 LangGraph，在文档检索中使用 ChromaDB。当前版本聚焦本地可复现、租户边界、结构化输出、可审计工具调用和评测证据，不把 mock 或固定评测集结果描述为生产业务效果。

## 项目矩阵

| 项目 | 业务场景 | 核心能力 | Fresh 验证 |
|---|---|---|---|
| [`ecommerce-multi-agent-support`](ecommerce-multi-agent-support/) | 跨境电商多 Agent 客服 | 商品咨询、售后处理、订单查询、物流追踪、JWT 店铺隔离、敏感动作待审批 | **97 passed**；eval **47/47** |
| [`business-data-agent`](business-data-agent/) | 跨境电商经营数据分析 | Amazon / TikTok Shop / Shopee 销售、广告、库存、竞品数据；Function Calling；SQL AST Guardrail | **64 core + 2 optional passed**；eval **8/8** |
| [`rag-financial-qa`](rag-financial-qa/) | 跨境电商商品文档 RAG | 商品手册、关税合规、物流单据；Citation Ledger；四类数值事实校验；三层 PDF 解析 | **378 passed**；活动集 **11 条 PASS** |

> `rag-financial-qa` 是历史兼容目录名。当前活动业务已迁移为跨境电商商品事实 RAG；历史金融 PDF Router、PaddleOCR 和真实评测证据仍按原结果保留。

## 总体架构

```text
[跨境电商用户 / 店铺运营]
            │ 1. JWT / 问题 / 文档
            ▼
[FastAPI API + 可信 shop_id 上下文]
            │
            ├──► [LangGraph 客服 Supervisor]
            │       ├─ 商品咨询 ─► Catalog Tool ─► Repository
            │       ├─ 订单/物流 ─► Order Tool ───► Repository
            │       └─ 售后处理 ─► Policy Tool ──► 待审批 Proposal
            │
            ├──► [经营数据分析 Agent]
            │       └─ Function Calling ─► sqlglot AST Guardrail
            │                              └─► shop-scoped SQLite
            │
            └──► [商品文档 RAG]
                    ├─ pdfplumber / hi_res / Paddle artifact
                    ├─ ChromaDB 文本与表格检索
                    └─ Citation Ledger + verified_v3 fail-closed
```

## 核心工程能力

### 1. 多 Agent 客服

外部路由契约固定为：

```text
product_inquiry
aftersales_handling
order_query
logistics_tracking
unsupported
```

该契约在接口和评测中由 Pydantic/Literal 约束。

- JWT 同时携带 `sub + shop_id`，并与数据库用户记录复核。
- 商品按 `shop_id` 隔离；订单和物流按 `shop_id + user_id + order_no` 查询。
- 支持 USD、EUR、GBP 和 LA、Berlin、London 店铺时区。
- 退款、取消订单、修改地址只生成 `requires_approval=true` 的待审批方案，不执行真实动作。
- 地址文本在本地确定性短路，避免姓名、电话和完整地址进入外部模型。
- 跨币种预算不做隐式换算，币种与店铺不匹配时 fail closed。

详见 [客服项目 README](ecommerce-multi-agent-support/README.md) 和 [架构文档](ecommerce-multi-agent-support/docs/ARCHITECTURE.md)。

### 2. 经营数据分析 Agent

内置跨境电商事实表：

- `sales_records`
- `ad_performance`
- `inventory_snapshots`
- `competitor_prices`

典型问题：

- 广告 ROAS / ROI 趋势
- 按 marketplace、currency 分区的选品贡献排名
- 库存周转、断货和库存未知风险
- 自有商品与竞品的价格差和价差率

安全边界：

- `sqlglot` AST 只允许单条只读 SELECT。
- 拦截 DDL/DML、多语句、SQL 注释、副作用函数和不可验证 LIMIT。
- 在每个业务 Select scope 注入绑定参数 `:shop_id`。
- 金额聚合必须保留 marketplace 和 currency；排名必须按市场和币种分区。
- User、DataSource、AnalysisRecord、Repository 和 connector cache 均使用 `user_id + shop_id`。

详见 [数据 Agent README](business-data-agent/README.md) 和 [项目概览](business-data-agent/docs/PROJECT_OVERVIEW.md)。

### 3. 商品文档 RAG

当前只发布四类可验证数值事实：

| Fact type | 必需证据 |
|---|---|
| `price` | 数值 + 明确币种 |
| `inventory_quantity` | 非负整数 |
| `delivery_duration` | 数值 + hour/day/business_day |
| `customs_duty_rate` | 数值 + percent |

文档处理链保持纯文本和表格方案：

```text
PDF L1: pdfplumber 全页正文
    └─► L2: Unstructured hi_res 候选表格页
            └─► L3: validated PaddleOCR artifact
                    └─► ChromaDB 文本/表格检索
                            └─► Citation Ledger + 结构化事实校验
```

- 不引入 ColPali、多模态模型或图像 embedding。
- 同一局部证据必须绑定 SKU、商品、平台、市场、日期、数值和单位/币种。
- 未知 citation、跨片段拼接、歧义多值、额外数字、资料外 SKU 或超范围事实均拒答并返回 `sources=[]`。
- API 与 document worker 分离；上传只入队，worker 完成解析和索引发布。

详见 [RAG README](rag-financial-qa/README.md)、[技术概要](rag-financial-qa/PROJECT_SUMMARY.md) 和 [Demo Runbook](rag-financial-qa/docs/DEMO_RUNBOOK.md)。

## 技术栈

- Python 3.11 / 3.12
- FastAPI、SQLAlchemy、Pydantic
- SQLite（默认）与可选 MySQL 元数据库
- Redis 本地编排预留（当前业务代码未接入）
- LangGraph `StateGraph`
- LangChain 对照 Demo
- ChromaDB、pdfplumber、Unstructured、PaddleOCR artifact
- sqlglot AST Guardrail
- pytest、JSONL evals
- Docker / Docker Compose

没有引入 PostgreSQL、Celery、Kafka、ColPali 或视觉大模型。MySQL 只作为数据 Agent 元数据库的可选部署，Redis 只在本地 Compose 中预留，不参与当前业务状态或任务执行。

## 快速开始

建议每个项目使用独立虚拟环境。

### 多 Agent 客服

```powershell
cd ecommerce-multi-agent-support
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evals\run_eval.py
.\.venv\Scripts\python.exe scripts\smoke_demo.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8002
```

Swagger：`http://127.0.0.1:8002/docs`

### 经营数据分析 Agent

```powershell
cd business-data-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evals\run_agent_eval.py --json
.\.venv\Scripts\python.exe scripts\smoke_demo.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001
```

Swagger：`http://127.0.0.1:8001/docs`

可选 MySQL 元数据库与 Redis 本地基础设施：

```powershell
copy .env.compose.example .env
docker compose config --quiet
docker compose up -d mysql redis business-data-agent
```

业务事实数据源仍是 shop-scoped SQLite；Redis 当前只作为本地基础设施预留。

### 商品文档 RAG

```powershell
cd rag-financial-qa
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SECRET_KEY="replace-with-at-least-32-random-characters"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evals\run_eval.py --validate-only
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

文档上传后还需在另一个终端启动：

```powershell
cd rag-financial-qa
$env:SECRET_KEY="replace-with-the-same-random-secret"
.\.venv\Scripts\python.exe -m app.workers.document_worker
```

Swagger：`http://127.0.0.1:8000/docs`

也可在 `rag-financial-qa` 目录使用 Docker Compose 同时启动 API 与普通 document worker：

```powershell
copy .env.example .env
docker compose up --build
```

## 验证证据

三个项目在独立目录、独立 `app` 包上下文中运行：

| 项目 | pytest | 活动评测 / Smoke |
|---|---:|---|
| 多 Agent 客服 | 97 passed | 47/47；route/tool 100%；security 4/4；隔离 SQLite smoke 通过 |
| 数据分析 Agent | 64 core + 2 LangChain optional passed | 8/8；tool/SQL/row/answer/scope/safety 均为 100%；API smoke 通过 |
| 商品文档 RAG | 378 passed | 11 条活动集结构校验通过；发布边界预检 50 PASS / 0 FAIL |
| **合计** | **541 passed** | 固定本地工程合同，不代表生产业务指标 |

RAG 历史证据没有被当前电商结果覆盖：

- historical/disclosed Gate B：provisional `12/24`，缺独立人工 attestation。
- Gate C：真实执行但失败，`verified_v3=0/24 accepted`。
- 修复后的新 sealed holdout 尚未执行，默认配置继续保持 legacy / L3 disabled。

详见 [LangChain 对照说明](docs/LANGCHAIN_COMPARISON.md) 和 RAG 历史报告目录。

## 当前边界

- 本仓库证明的是可复现工程链路、权限边界和固定评测合同，不证明云模型准确率或生产 SLA。
- 客服敏感动作只有 proposal，没有审批持久化、恢复点和真实平台写回。
- 数据 Agent 的公开注册 `shop_id` 仅用于受控 Demo；生产租户入驻需要服务端邀请、审批或 membership 体系。
- RAG 的真实语义质量需要配置真实 Embedding/Chat 模型后独立评测。
- PaddleOCR GPU 全量运行使用单独锁定环境；仓库不提交虚拟环境、真实 PDF、OCR 缓存或大型 candidate corpus。
- 本地 `.env`、API Key、运行数据库、上传文件、Chroma 数据、缓存和日志均不应提交。
