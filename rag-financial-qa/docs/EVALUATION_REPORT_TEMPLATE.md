# RAG 评测报告模板

> 使用方式：每次配置真实模型后，跑一遍 `evals/run_eval.py`，把 Summary 和关键失败案例填到这里。不要把 mock mode 的结果当作真实语义效果。

## 1. 评测环境

| 项目 | 内容 |
|---|---|
| 日期 | 2026-__-__ |
| 模型 | 例如 `gpt-4o-mini` |
| Embedding 模型 | 例如 `text-embedding-3-small` |
| TOP_K | 3 |
| CHUNK_SIZE | 400 |
| CHUNK_OVERLAP | 80 |
| LEXICAL_WEIGHT | 0.35 |
| MIN_RELEVANCE_SCORE | 0.05 |
| 评测问题数 | 24 |
| 文档集 | `finance_summary_2024.txt`、`risk_notice.txt` |

## 2. 运行命令

```bash
cd demo
python evals/run_eval.py --kb-id <kb_id> --top-k 3
```

只评测检索：

```bash
python evals/run_eval.py --kb-id <kb_id> --top-k 3 --retrieval-only
```

## 3. Summary 结果

把终端最后的 Summary 粘贴到这里：

```text
total_cases:
answerable_cases:
refusal_cases:
errors:
retrieval_hit_rate@3:
source_support_rate:
refusal_accuracy:
answer_keyword_match_rate:
```

## 4. 指标解释

- `retrieval_hit_rate@3`：Top-3 检索结果是否命中预期文档或预期关键词。它衡量的是检索召回，不等于最终答案正确率。
- `source_support_rate`：返回给用户的 sources 是否包含支撑答案的资料。它衡量可追溯性。
- `refusal_accuracy`：资料外问题和投资建议问题是否被拒答。它衡量幻觉控制和安全边界。
- `answer_keyword_match_rate`：答案是否覆盖关键事实。它是轻量自动评估，不能替代人工判断。

## 5. 失败案例复盘

| case_id | 问题 | 失败现象 | 初步原因 | 优化方案 |
|---|---|---|---|---|
|  |  | 检索没命中 / sources 不对 / 未拒答 / 关键词缺失 |  |  |

复盘思路：

1. 如果 `retrieval_hit` 失败，先看 chunk 是否切得太碎或太粗。
2. 如果 sources 不对，检查重排权重和相关度阈值。
3. 如果 answer 缺关键事实，检查 Prompt 和上下文是否包含答案。
4. 如果资料外问题没有拒答，增加拒答样本、相似度阈值或业务护栏。

## 6. 参数调整记录

| 日期 | 参数变化 | 结果变化 | 结论 |
|---|---|---|---|
|  | `TOP_K: 3 -> 5` |  |  |
|  | `LEXICAL_WEIGHT: 0.35 -> 0.5` |  |  |
|  | `MIN_RELEVANCE_SCORE: 0.05 -> 0.1` |  |  |

## 7. 结果摘要

可以这样讲：

> 我没有只做主观演示，还准备了 JSONL 评测集。评测集包含资料内事实问题、风险问题、资料外问题和股价预测类拒答问题。脚本会统计 Top-K 检索命中率、sources 支撑率、拒答准确率和答案关键词命中率。这样可以区分问题出在检索、来源引用、拒答策略还是模型生成。

## 8. 当前结论

用 3-5 句话总结本轮评测：

- 检索链路：
- 拒答能力：
- sources 可追溯性：
- 主要短板：
- 下一步优化：
