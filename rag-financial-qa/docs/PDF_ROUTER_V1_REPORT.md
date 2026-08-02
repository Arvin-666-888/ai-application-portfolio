# PDF Router V1 验证报告

## 1. 结论与声明边界

项目已构建并验证三层金融文档智能问答中台原型。V1 在 5 份真实 2024 年中文 A 股年报上完成 L1 全物理页覆盖、候选页路由、离线 Paddle artifact 校验与投影、冻结 Embedding 检索、防泄漏评分和自动测试。

本报告不作以下声明：

- 不称“已落地 2026 主流方案”；
- 不称“生产级”；
- 不称“业务达标”；
- 不把 30 题开发集称为 holdout；
- 不把离线 L1+L3 artifact 投影称为离线重跑 L2；
- 不用新结果覆盖中文 Unstructured `5/30 -> 0/30` 或外文 `3/15 -> 4/15` 的历史证据。

`financial_v2` 仍非默认检索配置，必须显式启用。

## 2. 三层架构与数据流

```text
[用户上传金融 PDF]
    │ 1. FastAPI 后台线程读取 PDF
    ▼
[L1：pdfplumber 逐物理页正文]
    │ 输出：全页正文、物理页码、路由特征；始终作为保底内容
    ├──────────────────────────────► [L1 chunks / 索引]
    │ 2. 无外部标签的确定性规则选择候选页
    ▼
[L2：在线候选页 Unstructured hi_res]
    │ 输出：有效结构化表格；失败/占位/无有效表格时回退
    ▼
[L3：在线 validated Paddle artifact adapter]
    │ 输入：离线 worker 产物；只校验并转换，不运行 Paddle/GPU
    ▼
[L1 正文 + 每页选中的 L2/L3 表格 chunks]
    │ 3. Embedding、向量/词法召回、排序
    ▼
[回答生成、来源与物理页引用]
```

### L1：物理页正文保底

L1 使用 `pdfplumber` 逐物理页抽取正文，保留 `source`、PDF SHA-256、物理页码、route reasons 与 provenance。即使候选页的 L2/L3 无表或失败，只要 L1 有正文，正文仍保留在 corpus 中。

### L2：在线 Unstructured hi_res

L2 只处理路由选中的候选页，调用单页 Unstructured `hi_res` 表格解析。有效表格直接进入索引；异常、placeholder 或无有效表格触发可审计回退。L2 在线行为与 L2→L3/L1 fallback 由自动测试验证。

### L3：在线只消费 validated Paddle artifact

PaddleOCR/PP-StructureV3 在独立离线 venv 生成页级 artifact。FastAPI 进程中的 adapter 只按 PDF SHA、物理页、schema、内容摘要与 engine fingerprint 校验并转换产物，不 import Paddle，也不加载 GPU runtime。

生产 app 不加载 ground truth，没有固定 5 报告、30 题、4,125/1,167 chunks 等数据集常量。相应约束由 adapter、router、document service 与 routed corpus 自动测试覆盖。

### 离线评测口径

当前真实离线 frozen corpus 是 **L1 + validated L3 artifact 投影**。离线构建器声明：

- `ground_truth_loaded=false`；
- `api_called=false`；
- `paddle_imported=false`。

离线构建没有重跑 L2。因此，L2 的在线行为证据来自自动测试；真实离线 corpus/召回证据来自 L1+L3 artifact 投影。两者不能混写。

## 3. 中文真实数据与 artifact 审计

输入为 5 份 2024 年中文 A 股年报：格力电器、美的集团、贵州茅台、比亚迪、招商银行。

| 项目 | 结果 |
|---|---:|
| 报告 | 5 份 |
| L1 inventory / 覆盖 | 1,338 / 1,338 物理页 |
| 候选页 | 400（每份上限 80） |
| OCR artifact | 400/400 completed |
| artifact missing / invalid/drop | 0 / 0 |
| 有表 / 合法无表候选页 | 305 / 95 |
| 表格 | 601 |
| L3 chunks | 1,167 |
| strict table coverage | 26/30 |
| 30 题唯一目标页候选覆盖 | 20/20 |
| 目标页 missing / dropped | 0 / 0 |

候选选择规则显式声明 `uses_ground_truth=false`。coverage audit 是事后审计结果，不回流为 pipeline 输入。

### 新 frozen corpus

新的最终 `router_v1_frozen_l1_corpus_v2.json` 使用：

- 4,125 个冻结 L1 chunks；
- 1,167 个 validated L3 chunks；
- 合计 5,292 chunks。

该 corpus 状态为 `degraded`，共有 96 条解释：

1. 95 个候选页 artifact 已完成但没有表，属于合法可审计结果；
2. 1 条 legacy candidate manifest 缺少共享 router policy identity，构建器记录兼容 fallback。

同时 `missing_reason_count=0`、`dropped_reason_count=0`。因此 `degraded` 不等于失败，也不表示 artifact 丢失、非法或被静默丢弃。

## 4. 实现与测试证据

- V1 阶段当时的完整 pytest 记录：**175/175 passed**（历史阶段数，不代表当前提交；当前状态以 fresh `pytest` 输出为准）；
- 唯一 warning：Pydantic class-based `Config` 弃用提示；
- 项目 `.venv`：`pip check` 通过；
- 独立 Paddle OCR venv：`pip check` 通过；
- 生产 app：无 ground truth 加载、无 Paddle runtime import、无固定数据集常量；
- L2 在线解析、L3 fallback、L1 保底、物理页映射、artifact schema/fingerprint、动态表数和 query-only 防泄漏均有自动测试。

独立审查后的加固已进入最终 v2 artifact：loader 计算 routed L1 canonical identity，并要求它与 baseline 完全相同；旧 9,488-chunk corpus 会被拒绝。builder v2 在 artifact 中分别记录 L1/L3 layer SHA。L3 配置默认关闭，显式启用必须 pin 合法 engine fingerprint。artifact metadata 只记录相对 locator，不暴露本机绝对路径；没有尝试 L2 时不再错误写入 `fallback_from=L2`；原子 JSON 写入只使用一个同目录临时文件并在结束时替换/清理。

5 个评测脚本统一共享 `atomic_json`；Windows 同目标 8 线程、每线程 64 次写入，连续 3 轮测试均为 0 error，且无 `.tmp` 残留。

测试通过证明的是当前实现合同和回归行为，不等于并发、容量、SLA、安全运营或线上业务效果已经达到生产要求。

## 5. 完整历史结果链

### 中文主验收

| 阶段 | Corpus / Parser | Retrieval | Row-aware Recall@5 | MRR | Candidate Recall@50 |
|---|---|---|---:|---:|---:|
| Unstructured 全量旧臂 | pdfplumber 正文 | 历史 hybrid | 5/30（16.67%） | — | — |
| Unstructured 全量新臂 | hi_res 结构化内容 | 历史 hybrid | 0/30（0%） | — | — |
| Paddle legacy | L1 + Paddle 表格 | legacy | 7/30（23.33%） | 0.194444 | 14/30 |
| Router V1 | frozen L1 + validated L3 | financial_v2 | **14/30（46.67%）** | **0.251111** | **23/30** |

中文 Unstructured 全量 `5/30 -> 0/30` 的 `parse_failure_count=0`。这说明解析流程完成不代表检索效果达标；该失败记录必须和后续改善并列保留。

### 外文补充历史证据

5 份官方外文年度报告/10-K、462 物理页、15 条固定 ground truth 的真实 API 全量评测结果为：

- 旧臂：3/15（20.00%）；
- Unstructured hi_res 新臂：4/15（26.67%）；
- `parse_failure_count=0`；
- 完整运行约 46 分钟。

英文结果只作补充，不能替代中文 A 股业务主验收，也不能被新 router 结果覆盖。

## 6. 防泄漏、冻结身份与可复核性

候选阶段只读取 query-only 文件中的 `case_id`、`question`。任何 `pdf`、`metric`、`expected_value`、`expected_page` 或 `table_id` 字段都会被拒绝。

候选 artifact 明确记录：

- `ground_truth_loaded=false`；
- `api_called=false`；
- Embedding cache：5,319/5,319 命中；
- 维度：1,536；
- API 补嵌入：0；
- missing：0；
- invalid：0。

候选与 ranking 冻结以后，`06_score_retrieval_artifact.py` 才加载 ground truth 并执行 row-aware scoring。

| 身份 | SHA-256 |
|---|---|
| ranking SHA | `2711483dc023251a1e197371633a972aa3162abb7f0ec876bbf1c4a10c4588e6` |
| corpus v2 文件 SHA | `3fdfa19ac4d54ccfa056fc45e074c70c03c617ef6087fe2c1b1948b28c60661e` |
| candidate artifact v2 SHA | `179476d9a411af97daa656ebbd60d9d71d01d97255474e39ab2c1c577aaf8187` |
| score artifact v2 SHA | `6305ffd5668ad51972918a596d1e3df265d46094422fa231fb0a7e90f4fabcca` |
| config SHA | `7b386c26a3ba8e75c845dc9074f7ce49aea15a5fc287b3d4306d3b9323466a31` |
| candidate identity | `92fd6ef6ee7c2d46355e3169c58913a6f089166096446936bdb1ae90a362cdda` |
| policy fingerprint | `839996581d15ade3592ccd72014a911a71d545a9a993b8daafee828e703eeccb` |

这里的 corpus SHA 是最终 v2 corpus 文件哈希；builder v2 内另记录 canonical `l1_corpus_sha256` 与 `l3_corpus_sha256`，供 loader 校验层身份。candidate 与 score SHA 是最终 `*_v2.json` 文件哈希。

## 7. 只读验证与历史复现边界

在项目根目录执行以下只读检查；当前测试总数以 fresh `pytest` 输出为准：

```bash
python scripts/07_build_routed_corpus.py --validate-only
python scripts/pre_interview_check.py --skip-tests
python -m pytest -q -p no:cacheprovider --basetemp "${TMPDIR:-/tmp}/rag-router-v1-pytest"
```

V1 历史运行还曾生成 `router_v1_frozen_l1_corpus_v2.json`、`retrieval_router_v1_candidates_v2.json` 和 `retrieval_router_v1_row_strict_v2.json` 等本地 artifacts。它们未纳入本次正式提交白名单，且历史 candidate file-SHA 链存在已披露的不完整性，因此本报告保留当时指标与身份用于审计，不把这些文件设为公开关键流程依赖，也不建议在仓库内重建或覆盖。若需复现实验，输出必须写到项目目录之外的新 scratch 目录，并重新记录 fresh SHA 与终端结果。

## 8. V1 → V2 → V3

### V1：路由原型与冻结证据（当前）

解决：全页正文保底、候选页表格增强、validated artifact、安全的在线/离线边界、审计状态和防泄漏评分。

代价与边界：OCR 是离线批处理；没有持久化任务队列；30 题是开发集；`financial_v2` 非默认；46.67% 未达到可据此声称业务达标的证据强度。

### V2：持久化 OCR 与独立验收

实施：

- 持久化 OCR worker/queue；
- 以 `pdf_sha256 + physical_page + engine_fingerprint` 等组成幂等键；
- 记录 queued/running/completed/failed/stale 状态、有限重试与断点恢复；
- 冻结新公司、新年度 holdout，在查看答案前锁定 query、corpus、配置与 scorer；
- 验收默认链路，而不只显式启用 `financial_v2`。

代价：增加消息队列、状态存储、worker 部署、重试/死信和运营复杂度。

### V3：答案可信与生产治理

实施：

- rerank；
- 结构化答案；
- 数值、单位、年度和引用一致性校验；
- 证据不足拒答；
- 延迟、成本、权限、审计、监控与数据治理。

代价：更高时延、额外模型成本、规则维护和持续评测负担。

V3 后立即总结三版各自解决的问题、引入代价和适用边界，不继续制造 V4/V5。
