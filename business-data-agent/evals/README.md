# Agent 评测说明

固定评测集覆盖四类跨境电商业务问题：广告 ROAS/ROI、选品经营贡献、库存周转、竞品价差；同时覆盖 schema 理解与危险 SQL 拦截。

指标：

- `tool_match_rate`：是否按期望顺序调用工具。
- `sql_match_rate`：SQL 是否包含固定表、指标字段与币种分组。
- `row_match_rate`：当前店铺结果行数是否满足预期。
- `answer_match_rate`：回答是否包含每个 case 声明的非空业务口径关键词。
- `scope_match_rate`：回答是否披露实际时间范围、marketplace、currency 和 timezone 值。
- `safety_pass_rate`：写操作、多语句、注释绕过是否被拦截。
- `pass_rate`：综合通过率。

运行：

```bash
python evals/run_agent_eval.py
python evals/run_agent_eval.py --json
```

`--real-llm` 使用环境中的 API Key。默认 mock 模式只证明确定性后端工具链、SQL 安全、店铺隔离和口径，不证明真实模型效果。本轮 fresh mock 结果为 `8/8`，tool/SQL/row/answer/scope/safety 指标均为 `100%`。
