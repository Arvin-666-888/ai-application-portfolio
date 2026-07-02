# RAG 评测说明

这个目录用于展示项目不是只“能问答”，而是能用固定问题集检查检索、引用和拒答能力。

## 评测集

- `fixtures/finance_summary_2024.txt`：经营摘要、收入结构、毛利率、管理层展望。
- `fixtures/risk_notice.txt`：云资源成本、汇率、客户集中度、数据安全风险。
- `questions.jsonl`：24 条功能验收问题，覆盖资料内事实、原因解释、风险问题、跨文档综合、资料外问题和投资建议类拒答问题。

每条 case 建议包含：

- `category`：问题类别，例如 `financial_fact`、`risk_explanation`、`out_of_corpus`。
- `difficulty`：难度，当前使用 `easy` / `medium` / `hard`。
- `answer_type`：答案类型，例如 `fact`、`fact_with_reason`、`risk`、`refusal`。
- `expected_sources`：期望命中的来源文档。
- `expected_keywords`：答案应覆盖的关键事实。
- `expected_context_keywords`：检索上下文中应出现的关键依据。
- `should_refuse`：是否应该拒答。

## 指标

- `retrieval_hit_rate@k`：Top-K 检索结果是否命中期望来源或期望关键词。
- `source_support_rate`：返回给用户的 sources 是否能支撑答案。
- `refusal_accuracy`：资料外问题和投资建议类问题是否被拒答。
- `answer_keyword_match_rate`：资料内答案是否覆盖关键事实。

## 运行方式

### 评测集结构校验

不需要启动 API，也不需要创建知识库：

```bash
python evals/run_eval.py --validate-only
```

### 端到端评测

1. 启动 API。
2. 注册用户，创建知识库。
3. 上传 `fixtures` 里的样例文档，等待文档状态变为 `ready`。
4. 记录知识库 ID，运行：

```bash
python evals/run_eval.py --kb-id 1 --top-k 3
```

只检查检索链路：

```bash
python evals/run_eval.py --kb-id 1 --top-k 3 --retrieval-only
```

未配置 `API_KEY` 时，Embedding 和回答都使用 mock mode，结果只能证明流程跑通，不代表真实语义质量。如需评估真实语义质量，建议配置真实 Embedding 和 Chat 模型后再跑一次，并把 Summary 结果记录到项目文档里。
