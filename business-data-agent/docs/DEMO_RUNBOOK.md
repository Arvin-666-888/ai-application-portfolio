# Agent 演示与验证 Runbook

## 验证顺序

在 `business-data-agent` 目录执行：

```bash
pytest -q --basetemp=.pytest-migration
python evals/run_agent_eval.py --json
python scripts/smoke_demo.py
```

三项分别验证：

1. AST SELECT-only 与 shop scope、JWT、Repository、preview、记录和导出隔离；本轮 fresh pytest 为 `58 passed`。
2. 广告 ROAS/ROI、选品、库存周转、竞品价差与危险 SQL 固定评测；mock eval 为 `8/8`，tool/SQL/row/answer/scope/safety 均为 `100%`。
3. `shop_id=amazon-us` 的注册、登录、数据源、自然语言分析和 Markdown 导出；本轮 smoke 已通过。

## 手工 Swagger

```bash
uvicorn app.main:app --reload --port 8000
```

打开 `http://localhost:8000/docs`：

1. 注册和登录请求填写 `shop_id`、`username`、`password`。
2. 创建 SQLite 数据源，连接 `sample_data/sample.db`。
3. 调用 tables/schema/preview，确认只返回当前店铺行。
4. 询问：`2026 年广告 ROAS 和 ROI 趋势如何？`
5. 检查回答包含口径、`currency`、`marketplace` 与时间范围。
6. 检查 `tool_trace` 顺序为 `get_schema -> execute_sql`。
7. 使用另一店铺 Token 访问原记录和导出，预期 404。

## 真实模型边界

配置 API Key 后会进入真实 Function Calling 分支，但 SQL 最终仍经过同一 SELECT-only AST 与绑定参数 shop scope。真实模型输出有随机性，mock/eval 通过不等于真实模型质量达标；需要另行记录真实模型评测结果。
