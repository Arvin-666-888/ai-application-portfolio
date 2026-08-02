# 跨境电商经营数据分析 Agent

基于 FastAPI + Function Calling 的多租户数据分析原型，面向 Amazon、TikTok Shop、Shopee 店铺的广告效率、选品、库存周转与竞品价格分析。

## 核心链路

```text
[店铺用户]
  │ shop_id + username 登录
  ▼
[JWT: sub + shop_id]
  │ user_id + shop_id
  ▼
[Router / Service / Repository]
  │ 双维度所有权校验
  ▼
[DatabaseConnector cache: ds_id + user_id + shop_id]
  │ SELECT-only AST 校验
  │ 每个业务 Select scope 注入 :shop_id
  ▼
[SQLite 业务数据源]
  │ 绑定参数执行
  ▼
[回答 / 记录 / CSV / Markdown]
```

## 业务表与固定口径

所有表都有 `shop_id`、`platform`、`marketplace`、`currency`、`timezone`：

| 表 | 用途 |
|---|---|
| `sales_records` | 销量、销售额、退款、平台费、COGS；商品经营贡献 = gross_sales - refunds - platform_fees - cogs |
| `ad_performance` | 广告花费及归因销售、退款、平台费、COGS；ROAS = attributed_sales / ad_spend；ROI = (attributed_sales - attributed_refunds - attributed_platform_fees - attributed_cogs - ad_spend) / ad_spend |
| `inventory_snapshots` | 当前库存、30 天平均库存与近 30 天销量；周转率 = trailing_30d_units_sold / average_inventory_units_30d；周转天数 = 30 / 周转率 |
| `competitor_prices` | 自有价与竞品价；价差 = own_price - competitor_price |

金额必须按 `currency` 分组，禁止跨币种直接聚合、比较或排名。日期按记录中的 `timezone` 解释。

## 安全与租户隔离

- `parse_and_guard_sql` 保留单语句、SELECT-only、危险节点/关键字、注释、副作用函数和 LIMIT 规则。
- 独立 `enforce_shop_scope` 使用 sqlglot AST，在 alias、JOIN、CTE、subquery、UNION 的每个业务 `Select` scope 注入 `:shop_id`。
- `shop_id` 由 JWT 服务端上下文提供，通过 SQLAlchemy `text()` 绑定参数执行，不接受用户覆盖，也不字符串拼接。
- 未知表、没有可施加业务 scope 的查询、缺失租户上下文均 fail closed。
- User、DataSource、AnalysisRecord、Repository、connector cache、preview、分析记录和导出均使用 `user_id + shop_id`。

## 本地运行

```bash
pip install -r requirements.txt
pytest -q --basetemp=.pytest-migration
python evals/run_agent_eval.py --json
python scripts/smoke_demo.py
uvicorn app.main:app --reload --port 8000
```

未配置 `API_KEY` 时使用确定性 mock mode；真实模型模式仍经过同一 SQL 执行边界。

Fresh 本地验证：`58 passed`；固定 mock eval `8/8`，tool/SQL/row/answer/scope/safety 指标均为 `100%`；API smoke 完成注册、登录、店铺数据源、广告分析和 Markdown 报告导出。已跟踪 `sample_data/sample.db` 通过只读完整性与 schema contract，重复 seed 检查保持文件 SHA 不变。

注册和登录请求都必须包含店铺：

```json
{"shop_id":"amazon-us","username":"demo_user","password":"password123"}
```

添加内置数据源：

```json
{
  "name":"内置跨境电商样例库",
  "db_type":"sqlite",
  "connection_string":"sqlite:///./sample_data/sample.db"
}
```

推荐问题：

- 2026 年广告 ROAS 和 ROI 趋势如何？
- 哪些商品的经营贡献更适合继续选品？
- 库存周转和断货风险如何？
- 我们的商品与竞品价差是多少？

## API

- `POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me`
- `GET|POST /api/datasources`、schema、tables、preview、delete
- `POST /api/analysis/ask`
- `GET /api/analysis/records`、详情、CSV、Markdown 导出

## 边界

- 当前仅支持 SQLite，不引入 MySQL、Redis、Adapter 层或 Shop 表。
- V1 的注册请求携带 `shop_id` 只用于受控 Demo 与预配置店铺；它证明 JWT/Repository/SQL 的行级隔离，不等同于生产租户入驻授权。生产环境必须由服务端邀请、管理员审批或成员关系分配店铺，不能信任公开注册者自报的 `shop_id`。
- 内置三店样例共享一个数据库用于验证行级隔离，不代表生产数据规模或模型质量。
- 应用层 AST guardrail 不能替代生产只读数据库账号、查询超时、资源配额、审计与密钥管理。
- 当前无汇率表，因此跨币种折算明确不支持。
- RAG 仅保留外部 HTTP 对接点，不内置向量库。
