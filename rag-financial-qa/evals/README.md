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

## Router V2 独立 Holdout（历史运行已解封）

`router_v2_holdout/` 的 4 份官方巨潮资讯中文 A 股年报与 24 条 query-only 问题曾在 GT 前冻结；`gate-b-20260729-r1` 已完成 pre-GT 全链，但随后仅用 AI 草稿产生 provisional 评分，因此原 holdout 已解封，不再是可用于规则修复后 official 声明的独立集。

历史结果必须保留：financial_v2 provisional row-aware Recall@5 为 12/24，缺独立人工 attestation；V3 同批 contexts 的 Gate C 已真实失败。新代码另外报告 `evidence_bindable_at_5`，用于区分“页/行命中”和“单个局部证据可安全绑定答案”，两者不能互相替代。

正式发布验收必须使用新的 sealed holdout，并在 GT 出现前完成 Gate B candidate/freeze。Ground Truth 和 attestation 使用：

- `common/router-ground-truth-v2.schema.json`
- `common/router-ground-truth-attestation-v2.schema.json`
- `common/ground_truth.template.json`
- `common/ground_truth_attestation.template.json`

代码只能校验 reviewer 声明、case identity 和 exact-byte SHA；独立人工复核本身必须由真实复核人完成。公开流程以本文件、`evals/common/` 合同和 [`../docs/PDF_ROUTER_V3_DESIGN.md`](../docs/PDF_ROUTER_V3_DESIGN.md) 为准；内部 handoff 不随公开源码提交。

## PDF Router V1：中文主验收

`questions.jsonl` 只服务于 TXT fixture，不能替代 PDF 表格验收。当前 PDF 主验收使用 5 份真实 2024 年中文 A 股年报、1,338 个 PDF 物理页和 30 条页级数值问题。

### 评测链路与数据边界

```text
[5 份中文年报 / 1,338 物理页]
    │ L1：pdfplumber 全物理页正文保底
    ├──────────────► [4,125 L1 chunks]
    │ 无 GT 规则选 400 候选页
    ▼
[在线 L2：Unstructured hi_res 候选页行为由自动测试验证]
    │ L2 无有效表格时可回退
    ▼
[在线 L3：只消费 validated Paddle artifact，不加载 GPU/Paddle]
    │ 离线 frozen corpus 实际投影
    ▼
[4,125 L1 + 1,167 L3 = 5,292 chunks]
    │ query-only、cache-only 候选检索
    ▼
[冻结 ranking + 独立 row-aware scorer 后加载 GT]
```

重要边界：离线真实 corpus 是 L1 加已验证 L3 artifact 的投影，**没有离线重跑 L2**。L2 在线分支及 L2→L3 回退由自动测试覆盖。生产 FastAPI 只读取 validated artifact，不在进程内加载 Paddle/GPU；生产应用代码也不加载 GT 或固定数据集常量。

数据审计：

- L1：5 报告、1,338/1,338 物理页覆盖；
- 候选页：400 页，选择规则声明 `uses_ground_truth=false`；
- OCR artifact：400/400 完成，0 missing、0 invalid/drop；305 页有表，95 页合法无表；
- 表格：601 张，1,167 个 L3 chunks；严格同报告/同物理页/同指标/同数值覆盖 26/30；
- frozen corpus：4,125 L1 + 1,167 L3 = 5,292 chunks；
- 30 题对应 20 个唯一目标页，候选覆盖 20/20，0 missing、0 dropped；
- 另保留 1 条 legacy candidate policy identity 兼容记录。

因此 routed corpus 的 `degraded` 是可解释审计状态：95 个候选页已成功处理但没有表，加 1 条 legacy identity 兼容记录；不是缺产物或损坏，`missing_reason_count=0`、`dropped_reason_count=0`。

### 结果与历史证据（不得互相覆盖）

| 阶段 | 结果 | 证据边界 |
|---|---:|---|
| 中文 Unstructured 全量旧臂 | 5/30（16.67%） | pdfplumber 基线 |
| 中文 Unstructured 全量新臂 | 0/30（0%） | hi_res 结构化臂；`parse_failure_count=0`，真实失败必须保留 |
| Paddle legacy row-aware | 7/30（23.33%） | L1 + Paddle 表格，legacy 排序 |
| `router_v1 + financial_v2` | **14/30（46.67%）** | MRR `0.251111`；Candidate Recall@50 `23/30` |
| 外文历史旧/新臂 | 3/15 → 4/15 | 5 份外文年度报告/10-K 补充证据，不替代中文验收 |

30 条中文问题已参与 smoke、coverage 与方案开发，是 **development set** 而不是 holdout。`financial_v2` 仍非默认配置，所以当前结果只支持“原型技术改善”，不支持“业务达标”“生产级”或“已落地 2026 主流方案”。项目可准确表述为：**已构建并验证三层金融文档智能问答中台原型。**

### 防泄漏与可复核身份

候选检索只加载 `development_questions.jsonl` 的 `case_id` 与 `question`：

- `ground_truth_loaded=false`；
- `api_called=false`；
- 5,319/5,319 个唯一文本缓存命中，Embedding 维度 1,536；
- API 补嵌入 `0`，missing `0`，invalid `0`；
- scorer 在候选与 ranking 冻结后才加载 `table_ground_truth.json`。

冻结身份：

| Artifact / 配置 | SHA-256 |
|---|---|
| ranking | `2711483dc023251a1e197371633a972aa3162abb7f0ec876bbf1c4a10c4588e6` |
| frozen corpus v2 | `3fdfa19ac4d54ccfa056fc45e074c70c03c617ef6087fe2c1b1948b28c60661e` |
| candidate artifact v2 actual file SHA | `7755529903233dbc3904724ff3fe800a001fe7cde3a6db34789c1768bbd02cf1` |
| candidate artifact v2 historical recorded identity | `179476d9a411af97daa656ebbd60d9d71d01d97255474e39ab2c1c577aaf8187` |
| score artifact v2 | `6305ffd5668ad51972918a596d1e3df265d46094422fa231fb0a7e90f4fabcca` |
| retrieval config | `7b386c26a3ba8e75c845dc9074f7ce49aea15a5fc287b3d4306d3b9323466a31` |
| candidate cache identity | `92fd6ef6ee7c2d46355e3169c58913a6f089166096446936bdb1ae90a362cdda` |
| router policy fingerprint | `839996581d15ade3592ccd72014a911a71d545a9a993b8daafee828e703eeccb` |

冻结身份来自独立审查修复后的 `*_v2.json` 最终文件。审查修复包括：loader 强制 routed L1 canonical identity 等于 baseline 并拒绝旧 9,488-chunk corpus；builder v2 写入独立 L1/L3 layer SHA；L3 默认关闭且启用必须 pin engine fingerprint；metadata 不记录绝对 artifact 路径；未尝试 L2 时不再错误标记 L2 fallback；写 JSON 时只生成一个同目录临时文件并原子替换。

5 个评测脚本统一共享 `atomic_json`；Windows 同目标 8 线程、每线程 64 次写入，连续 3 轮测试均为 0 error，且无 `.tmp` 残留。

完整解释见 [`../docs/PDF_ROUTER_V1_REPORT.md`](../docs/PDF_ROUTER_V1_REPORT.md)。

### 精确复现命令

在 Git Bash 中从 `rag-financial-qa` 项目根目录执行：

```bash
.venv/Scripts/python.exe scripts/07_build_routed_corpus.py --validate-only

.venv/Scripts/python.exe scripts/07_build_routed_corpus.py \
  --output evals/task2_paddleocr/chunks/router_v1_frozen_l1_corpus_v2.json

.venv/Scripts/python.exe scripts/05_evaluate_paddleocr_retrieval.py \
  --cache-only \
  --routed-corpus evals/task2_paddleocr/chunks/router_v1_frozen_l1_corpus_v2.json \
  --retrieval-profile financial_v2 \
  --questions evals/task2_paddleocr/development_questions.jsonl \
  --candidates-output evals/task2_paddleocr/reports/retrieval_router_v1_candidates_v2.json

.venv/Scripts/python.exe scripts/06_score_retrieval_artifact.py \
  --candidates evals/task2_paddleocr/reports/retrieval_router_v1_candidates_v2.json \
  --ground-truth evals/table_ground_truth.json \
  --output evals/task2_paddleocr/reports/retrieval_router_v1_row_strict_v2.json

.venv/Scripts/python.exe -m pytest -q
```

该阶段历史验证为 `175/175 pytest passed`；它不是当前全量测试口径。当前提交的最终测试数以 fresh clone 全量 pytest 输出为准；项目 `.venv` 与独立 Paddle OCR venv 的 `pip check` 均通过。

## 外文五报告历史补充证据

Alphabet、Amazon、Apple、Microsoft、NVIDIA 的 5 份官方年度报告/10-K 曾完成 462 物理页、15 条固定 ground truth 的真实 API 全量评测。最终公平口径为旧版 **3/15（20.00%）**、Unstructured hi_res 新版 **4/15（26.67%）**、`parse_failure_count=0`，完整运行约 46 分钟。

严格命中仍要求同一个 Top-5 chunk 同时包含正确报告、正确物理页、指标和数值边界。公开仓库保留 [`table_ground_truth_en_10k.json`](table_ground_truth_en_10k.json) 与 [`../docs/TASK2_ACCEPTANCE_REPORT.md`](../docs/TASK2_ACCEPTANCE_REPORT.md)；完整 compare 输出属于本地历史 artifact，不随源码提交。该补充结果与中文 `5/30 -> 0/30 -> 7/30 -> 14/30` 各自保留，任何新结果都不得覆盖历史失败证据。

## 三版完成边界

- **V1**：冻结三层路由、L1 保底、validated L3 artifact、可审计降级和无泄漏评分；历史失败与开发集改善并列保留。
- **V2**：持久化 OCR worker/queue、幂等恢复、精确 doc-version 索引和新公司/新年度 holdout 冻结协议已完成；正式 holdout score 与默认切换仍待独立 Ground Truth。
- **V3**：结构化答案、Citation Ledger、数值/单位/年度/公司/指标/口径 verifier、fail-closed 拒答和 RagRun 工程治理已完成；historical/disclosed holdout 的真实 API generation/score 已执行但 Gate C 失败，新的独立 sealed holdout 尚未执行。

V3 已完成工程 Gate A；2026-07-29 historical/disclosed holdout 的真实 generation/score 已执行但 Gate C 失败，新的独立 sealed holdout 尚未执行。它不等于 Gate B/V2 检索质量或 Gate C/V3 答案质量通过。三版到此总结，不继续 V4/V5。
