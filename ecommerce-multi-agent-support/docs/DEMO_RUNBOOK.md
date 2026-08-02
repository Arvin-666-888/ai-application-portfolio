# V1 Demo Runbook

## 自动验收

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evals\run_eval.py
.\.venv\Scripts\python.exe scripts\smoke_demo.py
```

预期：pytest `97 passed`；eval `47/47`，五路外部契约均被覆盖。`scripts/smoke_demo.py` 使用系统临时 SQLite，不读取或覆盖已有旧 schema 演示库。

## 核心演示

登录 `demo_user_01 / DemoPass123!` 后调用 `POST /api/v1/chat`。

1. 商品：`推荐一款 300 元以内的 65W 充电器`
   - `route=product_inquiry`
   - 商品均为 `shop-us`、`currency=USD`
2. 订单：`查询订单 VLT-2026-0001 的订单状态`
   - `route=order_query`
3. 物流：`订单 VLT-2026-0001 到哪里了`
   - `route=logistics_tracking`
   - 与订单路由使用同一个 `get_order_status`
4. 越权：查询 `VLT-2026-0002`
   - 订单、物流 facts 均为空
5. 地址变更：`订单 VLT-2026-0001 修改收货地址为 221B Baker Street`
   - `route=aftersales_handling`
   - `issue_type=address_change`
   - `proposed_action=address_change_review`
   - `requires_approval=true`
   - 订单状态不变，trace/audit 不出现完整地址

分别登录 `demo_user_02`、`demo_user_03` 可观察 EU/EUR/Berlin 与 UK/GBP/London 店铺语境。登录 Token 的 shop claim 必须与数据库用户记录一致。

## 边界

地址变更只是待审批提案，系统没有保存地址、创建审批记录或执行真实修改。只引用实测本地规则结果，不描述为生产平台效果。
