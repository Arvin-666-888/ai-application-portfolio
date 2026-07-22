# VoltCore 跨境电商多 Agent 智能客服系统

基于 LangGraph `StateGraph` 构建的可运行、可评测、可审计电商客服 V1.0。系统通过 Supervisor 将请求路由至商品导购、订单查询、售后处理或明确拒答分支，并把身份校验、数据库查询、硬规则和敏感动作控制保留在确定性代码中。

> V1.0 使用结构逼真的仿真商品、订单和物流数据，用于验证 Agent 协作与工程边界；不宣称真实商店业务效果。V2 计划引入真实政策文档 RAG 与 Checkpointer，V3 计划接入 WooCommerce 测试商店和 Human-in-the-loop 执行链路。

## 架构与数据流

```text
POST /api/v1/chat
        │
        ▼
JWT 鉴权：Token sub -> 可信 user_id
        │
        ▼
LangGraph Supervisor
        ├── catalog
        │     Catalog Agent -> search_products Tool
        │     -> CatalogRepository -> 商品事实回复
        ├── order
        │     确定性 Order Node -> get_order_status Tool
        │     -> order_no + user_id 归属校验 -> 订单/物流事实
        ├── aftersales
        │     Aftersales Agent -> get_order_status Tool
        │     -> evaluate_aftersales_policy Tool
        │     -> 待审批方案，不执行退款/取消/换货
        └── unsupported
              明确拒答，不调用业务 Tool
        │
        ▼
answer + facts + tool_trace + requires_approval
        │
        ▼
脱敏审计日志（按 JWT 用户隔离）
```

## V1.0 能力

- **LangGraph 多节点编排**：Supervisor 条件路由到 `catalog / order / aftersales / unsupported` 四类分支。
- **商品导购 Agent**：将自然语言转换为类目、预算、功率等结构化条件；Tool 对价格、库存和规格执行精确过滤，回答只引用真实 SKU、价格和库存。
- **确定性订单节点**：订单号提取不调用 LLM；Repository 使用 `order_no + JWT user_id` 查询，订单不存在与越权访问统一处理。
- **售后 Agent**：区分“用户陈述”与“系统物流事实”，组合订单、物流和 V1 固定政策，输出证据要求和待审批方案。
- **敏感动作控制**：退款、换货、补偿、取消、退货和质保只生成 `requires_approval=true` 的建议，不修改订单、不调用支付接口。
- **分层与可替换数据源**：Agent -> Tool -> Repository -> SQLite Adapter；V3 可增加 WooCommerce Adapter，不重写业务图。
- **可审计**：保存路由、工具名、结果数量、审批标记和事实是否存在；不保存 JWT 或完整用户消息。
- **可复现数据**：固定随机种子生成 12 个用户、50 个商品、100 个订单和 100 条物流记录。

## 实测结果

本地确定性 V1 路径提供：

- pytest：`55` 项单元、集成、安全和评测回归全部通过。
- 30 条 JSONL 离线评测，覆盖四类路由、工具选择、商品硬条件、订单越权、售后审批和提示注入。
- 评测实测：`30/30` 通过，路由准确率 `100%`、工具选择准确率 `100%`、安全案例 `4/4` 通过。
- 最新指标见 [`docs/V1_EVALUATION_REPORT.md`](docs/V1_EVALUATION_REPORT.md)。

评测边界：结果验证的是本地 `rule_fallback` 和确定性 Tool 链路，不代表云端模型准确率、线上流量或真实电商转化效果。

## 快速运行

要求 Python 3.12。依赖均使用精确版本。

```powershell
cd ecommerce-multi-agent-support
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evals\run_eval.py
.\.venv\Scripts\python.exe scripts\smoke_demo.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

服务启动后，可在另一个 PowerShell 窗口运行真实 HTTP 验收：

```powershell
.\.venv\Scripts\python.exe scripts\http_demo.py
```

打开 Swagger：`http://127.0.0.1:8002/docs`。演示账号为 `demo_user_01` 至 `demo_user_12`，密码统一为 `DemoPass123!`。

## Docker

```powershell
docker compose up --build
```

容器健康后访问 `http://127.0.0.1:8002/docs`。Docker 配置仅用于本地演示，生产环境必须替换 `SECRET_KEY` 并改用正式数据库与密钥管理。

## 主要接口

- `GET /health`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/chat`：正式 V1.0 多 Agent 接口
- `GET /api/v1/chat/audits`：查看当前用户的脱敏聊天审计
- `POST /api/v1/routing/preview`：仅查看 Supervisor 路由
- `POST /api/v1/chat/preview`：兼容旧演示接口，已标记 deprecated
- `GET /api/v1/products`
- `GET /api/v1/orders`
- `GET /api/v1/orders/{order_no}`
- `GET /api/v1/orders/{order_no}/shipment`

## 典型演示

```text
商品：推荐一款 300 元以内的 65W 充电器
订单：订单 VLT-2026-0001 到哪里了
售后：订单 VLT-2026-0015 的商品破损了，我要退款（demo_user_03）
越权：demo_user_01 查询 VLT-2026-0002
拒答：预测明天股票价格
```

详细步骤见 [`docs/DEMO_RUNBOOK.md`](docs/DEMO_RUNBOOK.md)。

## 项目结构

```text
app/
├── agents/       # Supervisor、Catalog、Aftersales 结构化决策
├── graph/        # StateGraph、条件边与业务节点接入
├── nodes/        # 确定性 Order Node
├── tools/        # 商品、订单和政策 Tool
├── ports/        # Repository Protocol
├── adapters/     # SQLite Adapter
├── services/     # 鉴权、政策、审计和种子数据
└── routers/      # FastAPI 接口
evals/            # 30 条 JSONL 与自动评测脚本
tests/            # 单元、集成、安全和评测回归
docs/             # 架构、演示、评测与面试材料
```

## 设计取舍

1. **为什么订单查询不是 Agent？** 输入、权限和结果都确定，普通节点更便宜、更快，也没有订单号幻觉风险。
2. **为什么 Agent 不直接写 SQL？** Agent 只产出结构化条件，Tool 和 Repository 控制查询能力与参数边界。
3. **为什么固定政策而不是 V1 就做 RAG？** V1 优先验证多 Agent 分工和安全闭环；真实政策文档、引用和多轮状态进入 V2。
4. **为什么不用知名品牌真实订单？** 无授权数据会削弱可信度；V1 明确使用仿真品牌，V2/V3 再增加公开数据和真实测试平台。

## Roadmap

- **V1.0（当前）**：路由、商品、订单、售后、拒答、审计、30 条评测、Docker。
- **V2**：LlamaIndex + ChromaDB 政策 RAG、来源引用、并行检索、LangGraph Checkpointer。
- **V3**：WooCommerce 测试商店、`interrupt` 人工审批、Webhook 验签和幂等 Action Executor。
