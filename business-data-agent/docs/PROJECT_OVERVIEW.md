# 跨境电商经营数据分析 Agent 技术说明

## 定位

该后端原型验证从店铺身份、自然语言问题、Function Calling、受控 SQL 到分析记录和导出的完整链路。业务范围固定为 Amazon、TikTok Shop、Shopee 的广告 ROAS/ROI、选品、库存周转和竞品价差。

## 实现

- FastAPI Router / Service + SQLAlchemy Repository 依赖注入。
- JWT 同时携带 `sub`（user_id）与 `shop_id`。
- User、DataSource、AnalysisRecord 直接保存 `shop_id`，不增加 Shop 表或 Adapter 层。
- connector cache 使用 `(ds_id, user_id, shop_id)`，连接串变化时重建 connector。
- `parse_and_guard_sql` 负责原 SELECT-only AST 规则；`enforce_shop_scope` 独立负责业务表行级隔离。
- 店铺谓词以 `:shop_id` 绑定参数施加到 alias、JOIN、CTE、subquery、UNION 的每个业务 Select scope。
- preview 也通过同一执行边界，记录详情、CSV 和 Markdown 导出均执行 `user_id + shop_id` 所有权过滤。

## 数据与口径

四张业务表均包含 `shop_id/platform/marketplace/currency/timezone`：

- `sales_records`：经营贡献 = gross_sales - refunds - platform_fees - cogs。
- `ad_performance`：ROAS = attributed_sales / ad_spend；ROI = (attributed_sales - attributed_refunds - attributed_platform_fees - attributed_cogs - ad_spend) / ad_spend。
- `inventory_snapshots`：30 天周转率 = trailing_30d_units_sold / average_inventory_units_30d；周转天数 = 30 / 周转率。
- `competitor_prices`：价差 = own_price - competitor_price；价差率 = 价差 / competitor_price。

所有金额分析必须保留 `currency`，没有汇率时不跨币种直接聚合。

## 验证

```bash
pip install -r requirements-dev.txt
pytest -q --basetemp=.pytest-migration
python evals/run_agent_eval.py --json
python scripts/smoke_demo.py
```

pytest 覆盖 SELECT-only、防绕过、每种 SQL scope、绑定参数执行、Repository 双维度隔离、JWT、数据源、preview、分析记录与导出。eval 覆盖四类业务问题与危险 SQL。smoke 覆盖注册、登录、数据源、分析和报告导出。

## 当前边界

- 默认元数据库与业务事实源使用 SQLite；可选 MySQL 只承载 User、DataSource、AnalysisRecord 元数据，Redis 仅本地 Compose 预留。
- V1 注册中的 `shop_id` 仅用于受控 Demo/预配置店铺，证明后续 JWT、Repository 和 SQL 行级隔离；它不构成生产级店铺归属证明。公开生产注册必须由服务端邀请、审批或 membership/角色关系授予店铺身份。
- mock mode 证明后端链路和确定性口径，不证明真实模型效果。
- 生产仍需数据库只读账号、超时、资源配额、审计、密钥轮换和 CORS 收敛。
