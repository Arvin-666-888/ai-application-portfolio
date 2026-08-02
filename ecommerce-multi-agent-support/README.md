# VoltCore 跨境电商多 Agent 智能客服系统

基于 LangGraph `StateGraph` 的可运行、可评测、可审计客服 V1。外部路由契约固定为：

`product_inquiry / aftersales_handling / order_query / logistics_tracking / unsupported`

## 架构与数据流

```text
[JWT：sub + shop_id]
    │ 校验用户数据库记录中的 shop_id / market / timezone
    ▼
[LangGraph Supervisor]
    ├─ product_inquiry ─► Catalog Agent ─► shop 商品 Repository
    ├─ order_query ─────► OrderStatusNode ─┐
    ├─ logistics_tracking ► 同一节点/Tool ─┴► shop + user + order_no 查询
    ├─ aftersales_handling ► 订单事实 + 固定政策 ► 待审批方案
    └─ unsupported ─────► 固定拒答
    ▼
[回答 + facts + 脱敏 tool_trace]
    ▼
[shop + user 隔离的脱敏 Audit]
```

## 已实现能力

- 商品查询按可信 `shop_id` 隔离，返回商品自己的 `USD / EUR / GBP` 币种。
- 订单与物流是两条外部路由，但复用现有 `OrderStatusNode`、`GetOrderStatusTool` 和 Repository 逻辑。
- Repository 使用 `shop_id + user_id + order_no` 校验订单/物流归属。
- JWT 同时包含 `sub + shop_id`；鉴权时必须与数据库用户记录匹配。
- 用户模型包含 `shop_id / market / timezone`；商品、订单含 `shop_id / currency`；审计含 `shop_id`。
- 地址变更归 `aftersales_handling`，产生 `address_change / address_change_review / requires_approval=true`。
- 地址变更仅是提案：不保存地址、不修改订单、不执行外部动作；完整地址不进入 trace 或 audit。
- 固定种子生成 12 用户、60 商品、100 订单、100 物流，覆盖：
  - `shop-us / USD / US / America/Los_Angeles`
  - `shop-eu / EUR / EU / Europe/Berlin`
  - `shop-uk / GBP / UK / Europe/London`

## 验证结果

- pytest：`97 passed`
- 本地确定性 eval：`47/47`，路由准确率 `100%`，工具选择准确率 `100%`，安全案例 `4/4`
- 隔离数据库 smoke：商品/订单/物流/售后链路通过，跨用户订单返回 404，退款只生成 `refund_review + requires_approval=true`
- 结果见 [`docs/V1_EVALUATION_REPORT.md`](docs/V1_EVALUATION_REPORT.md)

评测只证明本地 `rule_fallback` 和确定性 Tool 链路，不代表云端模型、生产流量或真实平台业务效果。

## 快速运行

```powershell
cd ecommerce-multi-agent-support
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evals\run_eval.py
.\.venv\Scripts\python.exe scripts\smoke_demo.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

Swagger：`http://127.0.0.1:8002/docs`。演示账号 `demo_user_01` 至 `demo_user_12`，密码统一为 `DemoPass123!`。

## 主要接口

- `GET /health`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/chat`
- `GET /api/v1/chat/audits`
- `POST /api/v1/routing/preview`
- `GET /api/v1/products`
- `GET /api/v1/orders`
- `GET /api/v1/orders/{order_no}`
- `GET /api/v1/orders/{order_no}/shipment`

## 典型演示

```text
商品：推荐一款 300 元以内的 65W 充电器
订单：查询订单 VLT-2026-0001 的订单状态
物流：订单 VLT-2026-0001 到哪里了
售后：订单 VLT-2026-0015 的商品破损了，我要退款（demo_user_03）
地址：订单 VLT-2026-0001 修改收货地址为 <地址>
越权：demo_user_01 查询 VLT-2026-0002
```

详细步骤见 [`docs/DEMO_RUNBOOK.md`](docs/DEMO_RUNBOOK.md)。

## 边界

- 当前 SQLite 初始化使用 `create_all`，不迁移旧库；已有旧结构数据库需重建后再 seed。
- `requires_approval=true` 只表示待人工审核方案，不代表审批记录、恢复点或动作执行器已经存在。
- V2/V3 仍计划引入政策 RAG、Checkpointer、真实测试商店和幂等 Human-in-the-loop 执行链路。
