# Agent 演示与验证 Runbook

这份文档记录 Agent 项目的本地验证顺序，便于在不同环境中快速确认核心链路是否可运行。

## 1. 验证目标

Agent 项目需要确认以下能力稳定：

- `python scripts/smoke_demo.py` 可以跑通注册、登录、创建数据源、自然语言分析和报告导出。
- `pytest` 可以通过核心自动化测试。
- `python evals/run_agent_eval.py --json` 可以输出固定评测结果。
- Swagger 可以完成登录、创建数据源、提问，并展示 `answer`、`sql_query`、`data`、`tool_trace`。
- SQL 只读限制、危险 SQL 拦截、最大工具调用步数、工具结果压缩和兜底回答逻辑可复核。

## 2. 推荐验证顺序

1. 运行 `python scripts/smoke_demo.py`，确认无 API Key 时也能跑通后端链路。
2. 运行 `python evals/run_agent_eval.py --json`，确认工具选择、SQL 结构、行数和安全拦截符合预期。
3. 运行 `pytest`，确认安全逻辑、权限隔离和报告导出没有回归。
4. 启动 Swagger，手工验证注册、登录、创建数据源、提问和导出接口。
5. 查看一次 `tool_trace`，确认 Agent 先读取 schema，再执行只读 SQL，最后保存结果。

推荐问题：

- `2024 年每月收入趋势如何？`
- `各产品线毛利率是多少？`

## 3. 真实模型分支

配置 `API_KEY` 后，项目会走真实 Function Calling 分支。真实模型可能多次调用 `get_schema`、`preview_table`、`execute_sql` 等工具。

工程控制点：

- `MAX_AGENT_STEPS` 限制最大工具调用步数，避免 Agent 无限循环。
- 工具结果会做长度压缩，减少长 schema 或预览数据对模型的干扰。
- 如果达到最大步数但 SQL 已执行成功，系统会基于已有结构化结果返回阶段性分析，而不是直接丢弃结果。

## 4. 后续扩展方向

- 将 `query_rag` 从预留接口扩展为稳定的文档检索工具。
- 引入 SQL AST 校验、表字段白名单和只读数据库账号。
- 增加查询超时、资源配额、审计日志和异常告警。
- 扩展评测集，覆盖更多业务问题和工具选择路径。
