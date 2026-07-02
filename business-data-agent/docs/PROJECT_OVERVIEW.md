# 企业经营数据智能分析 Agent 技术说明

## 项目定位

这是一个经营数据分析 Agent 后端原型，重点验证自然语言问题到工具调用、SQL 查询、结果解释、图表生成和报告导出的完整链路。项目保留 mock mode 和真实模型分支，便于在没有 API Key 的情况下复现后端流程。

## 核心能力

- 基于 FastAPI、SQLAlchemy 和 OpenAI-compatible Function Calling 实现经营数据分析 Agent。
- 通过 `get_schema`、`execute_sql`、`generate_chart`、`list_tables`、`preview_table` 等工具完成多步分析。
- 围绕收入趋势、产品线毛利率、预算执行、应收账款风险和现金流构建财务经营样例库。
- 支持 SQL 查询、图表生成、分析记录保存、CSV 导出和 Markdown 报告导出。
- 实现 SQL 安全控制：仅允许 SELECT，拦截危险关键字、多语句和注释，自动追加 LIMIT。
- 保存 `tool_trace`，记录工具名称、参数、执行状态和结果摘要，方便复核和排错。

## 运行与验证

```powershell
pip install -r requirements.txt
pytest
python scripts/smoke_demo.py
python evals/run_agent_eval.py
uvicorn app.main:app --reload --port 8000
```

验证重点：

- `scripts/smoke_demo.py` 验证注册、登录、创建数据源、自然语言分析和报告导出。
- `evals/run_agent_eval.py` 验证工具选择、SQL 结构、结果行数和危险 SQL 拦截。
- `pytest` 覆盖 SQL 安全、鉴权、数据源权限隔离、分析链路和报告导出。
- Swagger 可用于手工验证登录、数据源创建、提问、结果详情和导出接口。

## 技术取舍

- **受控工具执行**：模型只返回工具调用意图，数据库访问由后端工具层统一执行。
- **先 schema 后 SQL**：让模型先了解真实表结构，降低编造表名和字段的概率。
- **只读 SQL**：数据分析场景只需要查询，不应允许模型修改业务数据。
- **工具轨迹**：保存 tool trace，便于复核分析过程和定位工具调用问题。
- **mock mode**：用于验证后端链路；真实模型分支用于验证完整 Function Calling 能力。

## 当前边界

- 默认样例数据使用 SQLite；MySQL 连接形式已预留，但未做多数据库方言专项适配。
- SQL 安全是规则级防护，生产环境还需要 SQL AST、只读账号、表/字段白名单、查询超时、审计日志、结果脱敏和资源配额。
- RAG 工具通过 HTTP 调外部服务，当前项目只保留对接点，不内置向量库和文档索引流程。
- 前端暂未实现，当前通过 Swagger、脚本和导出报告完成验证。
