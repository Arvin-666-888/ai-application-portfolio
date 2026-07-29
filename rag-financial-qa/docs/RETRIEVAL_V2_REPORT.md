# 中文财报检索 V2 开发集报告

## 结论

金融四通道检索在当前 30 题中文财报开发回归集上，将 Paddle 表格增强语料的 row-aware Recall@5 从 `7/30 = 23.33%` 提升到 `14/30 = 46.67%`，绝对增加 `23.33pp`。这证明候选分流与确定性排序方向有效，但该数据集已经参与 smoke、coverage 和方案设计，因此结果仍属于技术验证，不能据此声称产生了业务价值。

## 数据流

```text
[30 条 query-only 问题]
        │ 仅 case_id / question
        ├────────► [5,319 条冻结 Embedding 缓存]
        │                 │ 0 API call
        ▼                 ▼
[旧文本 4,125 chunks] + [Paddle 表格 1,167 chunks]
        │
        ├── table dense 50
        ├── table lexical 50
        ├── text dense 30
        └── text lexical 30
                 │
                 ▼
       [Weighted RRF, k=60]
                 │
                 ▼
[指标同行 / 报表口径 / 年份 / 公司 / content type]
                 │
                 ▼
     [同 table_id 只保留一个 chunk]
                 │ 候选冻结并记录 SHA
                 ▼
         [独立 row-aware scorer]
```

## 同口径结果

| Arm | 语料 | 检索器 | Row-aware Recall@5 | MRR | Candidate Recall@50 |
|---|---|---|---:|---:|---:|
| A | 旧文本 | legacy | 4/30 (13.33%) | 0.1167 | 4/30 |
| B | 旧文本 + Paddle | legacy | 7/30 (23.33%) | 0.1944 | 14/30 |
| C | 旧文本 | financial_v2 | 4/30 (13.33%) | 0.1167 | 12/30 |
| D | 旧文本 + Paddle | financial_v2 | 14/30 (46.67%) | 0.2511 | 23/30 |

拆分效果：

- 当前 data-only（B-A）：`+10.00pp`
- V2 data-only（D-C）：`+33.33pp`
- retrieval-only（D-B）：`+23.33pp`
- system-level（D-A）：`+33.33pp`

## 根因与修复

V1 诊断显示，增强语料有 `25/30` 的 row-aware 正确证据进入 dense/lexical union，但只有 `7/30` 进入最终 Top-5；18 题在排序阶段丢失。V2 将表格与正文从共享候选池拆成固定四通道，避免正文密集候选挤掉表格，并用 RRF 消除 dense distance 与 lexical score 量纲差异。

V2 的 Candidate Recall@50 从 `14/30` 提升到 `23/30`，说明主要收益不仅来自最终 rerank，也来自独立通道配额。Top-5 仍有 13 题在排序阶段丢失，3 题在当前四通道候选中没有 row-aware 正确证据。

## 可信边界

- 检索阶段使用 `development_questions.jsonl`，只允许 `case_id` 和 `question`；含 expected page/value、正确 PDF 或 table ID 会被拒绝。
- 5,319/5,319 缓存命中，missing=0、invalid=0、API call=0。
- 候选检索重复两次的 ranking SHA 均为 `5e6d56ea0cb5028662a0675806a03a77747d166a29ade8ae434adf524909c46c`。
- row-aware scorer 修复了“指标和值在同 chunk 但不同行”的形式假阳性，并在年份表头可可靠识别时校验正确年份列。
- V2 没有更改 ground truth、重新 OCR 或重新请求 Embedding。
- legacy 的 7 个命中中，V2 保留 5 个，case 00 和 case 18 分别降至 rank 6 和 rank 8；净增 7 题。由于当前开发集已经达到预设停止线，本轮不针对这两题调权重，避免过拟合。
- 应用默认仍为 `RETRIEVAL_PROFILE=legacy`；`financial_v2` 仅显式启用，等待独立 holdout 验收。

## 业务价值判断

当前结论仍是“技术验证显著改善”，不是“已经产生业务价值”。下一阶段必须冻结新公司/新年度的独立问题集，并在查看答案前固定 query、语料和配置 SHA。建议项目级最低门槛为总体 Recall@5 ≥50%，新公司与新年度两个子集各 ≥40%；有人复核的真实试点仍建议 Recall@5 ≥80%，自动化财务场景建议 ≥90%–95%。
