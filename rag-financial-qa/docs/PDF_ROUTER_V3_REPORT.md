# PDF Router V3 工程验证报告

## 1. 结论

截至 2026-07-29，V3“可信答案与原型级生产治理”已完成工程实现、本地验证和一次冻结上下文真实 API 运行：结构化财务答案、请求内 Citation Ledger、Decimal 数值/单位/币种/年度/公司/指标/口径校验、证据不足拒答、`verified_v3` 同步/SSE 统一可信管线、legacy SSE 增量兼容、精确 doc-version 查询、旧索引清理、RagRun 审计、token/估算成本、所有权与基础安全治理均已接入。

准确声明：

> V3 工程原型完成；historical/disclosed Router V2 holdout 的 pre-GT 链和 V3 真实 API generation 已执行，但评分只使用 AI 盲标草稿且缺少独立人工 attestation，因此均为 provisional。Gate B provisional 指标为 `12/24`，但不能 official finalize；Gate C 真实运行失败，`verified_v3=0/24 accepted`。根因修复后的新 sealed holdout 尚未执行，默认继续保持 legacy、L3 disabled、verified_v3 opt-in。

## 2. 本轮真实验证

### 自动测试（历史阶段记录）

以下固定数量均是 2026-07-29 对应阶段的留档，不代表当前提交；当前测试状态必须引用本轮 fresh `pytest` 终端输出。

原 Gate A 当时的全量记录为：

```text
296/296 passed
```

本轮新增/相关 Gate B、Paddle artifact、retrieval、Gate C 与 RAG V3 回归：

```text
111/111 passed
```

覆盖：

- V1/V2 解析、artifact、worker、索引与评测回归；
- 精确 `(doc_id, index_version)` 可见性和 legacy 交叉污染反例；
- finalize 旧版本清理成功/失败不回滚；
- request-scoped ParseRuntimeOptions；
- Decimal 规范化和同表格行/正文同句 verifier；
- unknown/missing citation、错值/单位/币种/年度/公司/指标/口径、跨片段、多值歧义和答案额外数值 fail closed；
- 同步 API、SSE 事件顺序、RagRun 落库和跨用户 run 404；
- migration、evidence guard 和 stale index cleanup。

warning 仅包括项目既有 Pydantic class-based Config 弃用提示，以及 TestClient 上游弃用提示；没有测试失败。标准测试命令使用进程级临时随机 `SECRET_KEY`，不读取或修改本地 `.env` 中的密钥。

### 低成本验证

- 主 `.venv` `pip check`：通过；
- 24 条功能评测集 `--validate-only`：通过（16 answerable、8 refusal）；
- Router V2 holdout freeze：通过，4/4 报告，24 题，`ground_truth_loaded=false`；
- Paddle artifact `--validate-only`：400 expected、400 completed、0 failed、0 missing；
- routed corpus `--validate-only`：5 报告、1,338 页、4,125 L1 + 1,167 L3 = 5,292 chunks，0 missing、0 dropped；
- Docker Compose config：通过；
- SQLite migration：首次 apply 创建 2 项、check 0 remaining、第二次 apply 0 operation；
- `git diff --check`：无 whitespace error。

### Historical/disclosed Router V2 holdout：真实 pre-GT 链与 provisional 评分

运行 `gate-b-20260729-r1`，全程使用新目录，未覆盖历史 canonical artifact：

- 冻结校验：24 题、4 份报告、934 页，PDF identity 4/4 通过；
- 通用规则选择 320 个 OCR 候选页，`uses_ground_truth=false`；
- PP-StructureV3 GPU：320/320 completed，0 failed/missing/stale，243 页有表、77 页合法无表，共 549 张表；
- routed corpus：2,827 L1 + 962 L3 = 3,789 chunks；
- `text-embedding-v2`：3,807 个唯一文本（含 24 个 query）全部真实写入新 cache，维度 1,536；
- pre-GT freeze 已锁定 corpus、parser、OCR engine、Embedding、retrieval config、candidate、ranking 和 scorer identity；
- AI 盲标草稿完成 24/24，明确标记 `human_review_status=pending`，其中 `holdout_05` 的“家用电器业务毛利率”口径需人工重点复核；未创建正式 Ground Truth 或 attestation。

provisional row-aware Recall@5：

| Profile | Overall | New company | New year |
|---|---:|---:|---:|
| legacy | 4/24 = 16.7% | 2/12 = 16.7% | 2/12 = 16.7% |
| financial_v2 | 12/24 = 50.0% | 7/12 = 58.3% | 5/12 = 41.7% |

四项 Gate B 数值条件在草稿口径下均命中，但 official blocker 为 `ground_truth_attestation_missing`，因此不得 finalize、不得切换默认检索配置。

### V3 historical/disclosed 上下文真实 API 运行（Gate C 失败）

运行 `v3-holdout-20260729-r1`，使用 Gate B 冻结的 `financial_v2` top-5 contexts，对 24 题分别执行 legacy 与 `verified_v3`，共 48 个 profile evaluation；其中 20 个 `verified_v3` evaluation 在 evidence preflight 阶段提前拒绝，实际发起 28 次 Chat API 请求，无 provider error。

- legacy：20 accepted / 4 refused，coverage 83.3%，accepted-answer strict precision 20.0%，P50/P95 9,790/22,873 ms，input/output tokens 54,412/13,358；
- `verified_v3`：0 accepted / 24 refused，coverage 0，strict precision 0，P50/P95 1/17,407 ms，input/output tokens 9,067/3,254；
- unknown citation acceptance = 0，unsupported numeric acceptance = 0，schema error rate = 0；
- 24 题中 20 题在 generation 前被 `evidence_preflight` 以 `no_fact_binding` 拒绝，仅 4 题进入结构化生成；
- 模型私有端点价格未配置，estimated cost 为 unavailable，成本门禁不可评估；
- Gate C 未通过，且同样缺少独立人工 attestation，未生成 finalized manifest。

根因抽查显示，检索并非完全缺证据：例如 `holdout_00` top-5 已包含第 121 页合并利润表原值，但 L3 chunk 未保留页级“合并利润表”标题/口径，preflight 又要求单个局部片段同时绑定指标、年度、口径和明确数值，导致 fail closed。`holdout_02` 还暴露三项通用 intent 缺陷：公司正则从“归属于母公司”截出异常公司词、所有权指标中的“母公司”被误判为报表口径、归母净利润被扩张成普通净利润 family。本次 holdout 已用草稿解封，禁止修规则后把重跑结果冒充本次 official 验收。

### 2026-07-29 根因修复（不改写上述历史结果）

后续工程修复已完成，但尚未产生新的独立质量结论：

- Paddle artifact v2 保留 PP-StructureV3 `parsing_res_list` 的 reading order/bbox/text anchor；新增 `financial-table-semantic-context-v1`，按具体表绑定同页标题、报表类型、scope、期间、单位和列语义，同页多表不共享整页 scope；跨页自动继承保持关闭，仅有相邻页和相似表头不足以形成可信 continuation；
- 每个 L3 chunk 重复受信任的 TableEvidence prefix、真实表头和当前行；legacy V1 artifact 可读但明确为 `legacy_unbound`，不能冒充 V2 semantic；
- `_local_evidence_segments()` 不再把 `[Table | source | page]` 当 Markdown header，并把已绑定单位/年度/scope 物化到单行 segment；preflight 的 fail-closed 条件没有放宽；
- `parse_query_intent()` 改为最长指标 family、protected span 和显式 scope phrase，修复 `holdout_02` 的异常公司词和“母公司”双义误判；多指标查询在当前产品边界内明确拒绝为 `unsupported_multi_metric`，不静默只回答其中一个；
- Gate B 新增 `evidence_bindable_at_5` 诊断；GT/attestation 升级为 v2 exact-SHA 合同；V3 candidate 只接受 Gate B official finalized pass，且 Gate B/C 必须使用同一 GT/attestation 字节。

这些改动只证明根因已在代码和 fixture 中处理。该阶段最后一次留档的全量回归为 `330/330 passed`，它是历史阶段数，不代表当前提交；当前状态必须引用 fresh `pytest` 输出。另经对抗性审查修复了无 schema stage 绕过 strict validator、Gate C 子串评分、手工 Gate B score finalize、跨页 scope 污染、伪 semantic digest 和多指标部分回答六类 fail-open。当前 `gate-b-20260729-r1`、`v3-holdout-20260729-r1`、provisional 分数和 Gate C failed 状态保持不变；未来 official 结论必须来自新的 sealed holdout 和真实独立人工复核。该新 sealed holdout 截至本报告尚未执行。

### 真实 FastAPI 启动验证

使用实际 Uvicorn 入口启动后：

- `GET /health`：200，`status=ok`；
- `GET /ready`：200，SQLite/Chroma 均 `ok`，无 pending job 时 `status=ready`；
- OpenAPI 包含：
  - `/api/chat/{conversation_id}`
  - `/api/chat/{conversation_id}/stream`
  - `/api/chat/runs/{trace_id}`

验证未调用真实 Chat/Embedding API，也未写入业务问答数据；服务已停止。

## 3. 实现结果

### 答案可信

- `RAG_ANSWER_PROFILE=legacy|verified_v3`，默认 `legacy`；
- 模型只输出候选 JSON；Pydantic 和确定性 verifier 决定是否发布；
- `verified_v3` 的同步与 SSE 共用可信决策；legacy SSE 保留增量 content 事件兼容；
- 生成前 evidence binding preflight，资料不足不调用 Chat 模型；
- 生成后任一一致性失败都返回稳定拒答，原答案不保存、不返回；
- 复杂公式明确不支持，不跨表推导。

### 检索与摄取一致性

- `active_index_targets` 精确过滤文档与版本；
- 兼容旧 `active_index_versions` API，但禁止两者同时传；
- 发布成功后清理旧版本，失败只记录 warning；
- 全局 Paddle settings 竞态已移除。

### 审计与治理

- `RagRun` 记录 trace、终态、reason、阶段耗时、tokens、estimated cost、doc-version targets 和配置身份；
- usage 缺失为 null，成本未配置为 unavailable；
- ordinary document worker 已加入 Compose；Paddle worker 仍为独立 Windows/GPU 环境；
- `/ready` 提供数据库、Chroma 和 pending job 的粗粒度状态；
- 所有权、Prompt Injection 数据边界、CORS、默认密钥、密码依赖和错误脱敏已收口到原型级边界。

## 4. 未完成与不得夸大的内容

- V2 独立人工 Ground Truth 和 attestation 尚不存在；historical/disclosed Gate B 的 `12/24` 只是在 AI 盲标草稿上的 provisional 指标；
- Gate C 已完成真实 API generation/score，但失败：`verified_v3=0/24 accepted`，coverage 0；
- 根因修复后的新 sealed holdout 尚未执行；
- 私有模型端点的 token 单价尚未配置，因此真实成本门禁不可评估；
- 没有多租户、RBAC、SSO、不可篡改审计、分布式队列、Prometheus/OTel 或生产 SLO；
- mock/API 自动测试证明工程契约；本次真实运行证明当前冻结链路未达发布门禁，不能据此切默认配置；
- 当前工作区含大量 V1/V2/V3 未提交文件，尚不能把远端干净 clone 描述为完整可复现版本。

## 5. 历史证据保护

历史结果原样保留：

- 中文 Unstructured：5/30 → 0/30；
- Paddle legacy：7/30；
- development financial_v2：14/30；
- 外文：3/15 → 4/15。

新增 evidence guard 将最终历史路径视为 immutable；V3 finalized run 也不可覆盖。candidate 历史 file-SHA 链不完整的问题继续如实记录，不伪造旧字节或手工改 score。2026-07-29 复核时，未跟踪的 `retrieval_router_v1_candidates_v2.json` 实际 file SHA 为 `7755529903233dbc3904724ff3fe800a001fe7cde3a6db34789c1768bbd02cf1`，而历史文档曾把 `179476d9a411af97daa656ebbd60d9d71d01d97255474e39ab2c1c577aaf8187` 标成 candidate 文件 SHA；本轮不改写该 artifact，只将两者明确区分，避免继续误报。
