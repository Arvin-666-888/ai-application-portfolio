# 简历项目说明：企业经营数据智能分析 Agent

## 项目定位

这是一个面向 AI 应用开发岗位的求职展示型项目，重点展示从自然语言问题到工具调用、SQL 查询、结果解释、图表与报告导出的完整链路。

项目不包装成生产级 BI 平台。更准确的定位是：一个具备工程结构、安全边界和可复现演示能力的 LLM 数据分析 Agent 原型。

## 推荐简历写法

**企业经营数据智能分析 Agent｜FastAPI / Function Calling / SQLAlchemy / SQLite**

- 基于 FastAPI、SQLAlchemy 和 OpenAI-compatible Function Calling 实现经营数据分析 Agent，支持通过中文自然语言查询业务数据库并生成分析结论。
- 设计 `get_schema`、`execute_sql`、`generate_chart`、`list_tables`、`preview_table`、`query_rag` 等工具，由 Agent 根据问题动态选择工具并完成多步分析。
- 围绕收入趋势、产品线毛利率、预算执行、应收账款风险和现金流构建财务经营样例库，支持 SQL 查询、图表生成、分析记录保存、CSV 和 Markdown 报告导出。
- 实现 SQL 安全控制和可追溯机制：仅允许 SELECT 查询，拦截危险关键字、多语句和注释，自动追加 LIMIT，并持久化 Agent 工具调用轨迹。
- 补充 pytest 自动化测试和 Agent 评测集，覆盖 SQL 安全、鉴权、数据源权限隔离、工具选择、SQL 结构、mock Agent 分析链路和报告导出，提升项目可复现性。

## 面试演示路径

1. 运行 `python scripts/smoke_demo.py`，展示无 API Key 情况下仍可跑通端到端链路。
2. 运行 `python evals/run_agent_eval.py`，展示工具选择、SQL 结构、结果行数和危险 SQL 拦截有固定评测集。
3. 运行 `pytest`，展示 SQL 安全和核心 API 有自动化验证。
4. 启动 `uvicorn app.main:app --reload --port 8000`，打开 `/docs` 演示注册、数据源、自然语言分析和报告导出。
5. 展示一次分析详情中的 `tool_trace`：说明 Agent 先获取 schema，再执行只读 SQL，最终保存结果。

## 可以主动讲的技术取舍

- **为什么不让模型直接连数据库？** 后端统一执行工具，先做 SQL 校验，再通过受控连接查询，降低误操作风险。
- **为什么保存 SQL 和工具轨迹？** 数据分析类 Agent 必须可解释，面试官和用户都能复核结论来自哪条 SQL、经过哪些工具。
- **为什么有 mock 模式？** 求职演示不能依赖外部模型服务稳定性；mock 模式保证现场能展示后端链路，真实 API Key 则展示完整 LLM 能力。
- **SQL 安全还有哪些生产改进？** 生产环境需要只读账号、权限隔离、查询超时、审计日志、SQL AST 解析、结果脱敏和资源配额。

## 当前边界

- 主要验证 Agent + 数据分析链路，不是生产级 BI 系统。
- 默认样例数据使用 SQLite；MySQL 连接形式已预留，但没有做多数据库方言专项适配。
- RAG 工具通过 HTTP 调外部服务，当前项目只实现对接点，不内置向量库和文档索引流程。
- 前端暂未实现，当前通过 Swagger、脚本和导出报告完成演示。
