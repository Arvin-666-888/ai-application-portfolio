# Agent 评测说明

这个目录用于把 Agent 项目从“能演示”推进到“能证明”。普通 pytest 主要防止代码回归；这里的 evals 用固定问题集检查 Agent 的工具选择、SQL 结果和安全边界。

## 文件

- `agent_questions.jsonl`：固定评测问题集。
- `run_agent_eval.py`：离线评测脚本，不需要先启动 uvicorn。

## 指标

- `tool_match_rate`：工具调用轨迹是否包含期望工具顺序，例如 `get_schema -> execute_sql`。
- `sql_match_rate`：生成或执行的 SQL 是否包含关键表、字段和聚合语句。
- `row_match_rate`：查询结果行数是否满足预期。
- `safety_pass_rate`：危险 SQL 是否被安全规则拦截。
- `pass_rate`：综合通过率。

## 当前覆盖的业务问题

- 月度收入趋势：按 `record_month` 聚合 `SUM(revenue)`。
- 产品线毛利率：按 `product_line` 聚合，口径为 `SUM(gross_profit) / SUM(revenue)`。
- Schema 理解：确认 Agent 至少会先读取数据库结构。
- SQL 安全：覆盖删除、多语句和注释绕过。

新增 Agent 能力时，建议先在 `agent_questions.jsonl` 增加评测 case，再改 Agent 分支或真实模型提示，最后运行 `python evals/run_agent_eval.py`。

## 运行

在 `demo` 目录执行：

```bash
python evals/run_agent_eval.py
```

输出 JSON：

```bash
python evals/run_agent_eval.py --json
```

使用真实模型配置：

```bash
python evals/run_agent_eval.py --real-llm
```

默认 mock 模式只用于证明后端工具链、SQL 安全和轨迹保存可以稳定跑通。如需评估真实模型效果，可以配置 API Key 后再补充一次真实模型模式的评测结果。
