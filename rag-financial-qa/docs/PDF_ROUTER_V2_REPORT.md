# PDF Router V2 验收报告

日期：2026-07-28

## 结论

**V2 工程基建已完成并通过本地验证；V2 独立质量验收尚未解封，因此默认链路不切换。**

可以准确表述：项目已具备 SQLite 持久 document/page job、独立 document/Paddle worker、租约与心跳、有限重试、stale recovery、人工 requeue、validated artifact、版本化 Chroma upsert 与 active-version 查询隔离。不能表述为“holdout 达标”“生产级”或“默认 `financial_v2` 已业务验收”。

## 完成矩阵

| 验收项 | 结果 | 证据 |
|---|---|---|
| 上传只持久入队，不创建 Web 进程内任务 | 通过 | `app/routers/documents.py` |
| 幂等 enqueue / 双 worker 原子 claim | 通过 | repository tests |
| lease / heartbeat / stale recovery | 通过 | repository + heartbeat tests |
| 有限重试 / failed / manual requeue | 通过 | repository tests |
| API/worker 重启后 job 仍在 SQLite | 通过 | SQLite queue + 本地 E2E |
| Paddle runtime 与 API 进程隔离 | 通过 | `app/workers/paddle_worker.py` 动态 worker 初始化 |
| artifact schema/identity/digest 兼容 V1 adapter | 通过 | Paddle artifact tests |
| finalize 等待 OCR 终态，失败页允许 L1 降级 | 通过 | worker tests |
| 版本化确定性 ID + Chroma upsert | 通过 | indexing tests |
| job-scoped staging + worker/attempt CAS 发布 | 通过 | publication fence tests + E2E |
| legacy Chroma/Document active version 迁移 | 通过 | migration/indexing tests |
| active index version 查询过滤 | 通过 | indexing + Chat 接线 |
| Embedding index/维度/finite 校验 | 通过 | document/indexing tests |
| completed artifact whole-file SHA + immutable snapshot | 通过 | worker artifact tests |
| 文档/知识库删除异常传播与递归 snapshot 清理 | 通过 | deletion review + tests |
| candidate/score canonical 不可覆盖 | 通过 | evidence protection tests |
| 新公司/新年度 holdout 冻结 | 通过（未解封） | freeze validator |
| holdout Recall@5 门槛 | **未执行** | Ground Truth 不存在 |
| 默认切换到 L3 + `financial_v2` | **未执行** | 门槛未判定 |

## 实测结果

以下记录属于 2026-07-28 V2 工程阶段，不代表当前提交的 fresh 测试总数；当前状态必须引用本轮 `pytest` 终端输出。

- 当时全量测试：`214/214 passed`。
- 主项目 `.venv`：`pip check` 无损坏依赖。
- 独立 PaddleOCR venv：`pip check` 无损坏依赖。
- migration：第一次 apply 1 项，第二次 apply 0 项，随后 check 无变更。
- 本地持久 worker E2E：
  - processed：`[true, true, false]`（ingest、finalize、队列空）；
  - jobs：`document_ingest_v2 completed attempt=1`、`document_finalize_v2 completed attempt=1`；
  - Document：`ready / ingestion completed / enrichment enriched_ready`；
  - active index：32 位 job-scoped staging version，经原始 worker + attempt SQL CAS 一次性发布；
  - chunks：2，Chroma count：2，metadata version 与 DB active version 一致。
- V1 冻结回归：400/400 OCR artifacts completed；1,338 L1 pages；601 tables；4,125 L1 + 1,167 L3 = 5,292 chunks；0 missing/drop。
- 既有评测集 validate-only：24 cases PASS。

## Holdout 冻结证据与后续披露

2026-07-28 冻结时状态为 `ground_truth_loaded=false`：

- query-only SHA：`c66207293f093f2559d2acd2a4be3de7ccdac0dbff9af905a9899f35db56ed19`
- preregistration SHA：`ed8fc08564edc222d395685ba992fb6d701422c0f5eba9596bf51e2e54339b00`
- source manifest SHA：`af93a24406ef49ecb1e979478218483739d7491aab450f4ca9e01892f6aaa2db`
- 4 份 PDF：海尔智家 2024（247 页）、五粮液 2024（147 页）、格力电器 2023（249 页）、美的集团 2023（291 页）。
- 24 题：新公司 12 题、新年度 12 题。

在该时间切片中，没有 `private/ground_truth.json`，也尚未产生任何 score；这解释了上表当时的“未执行”，不能再被当成项目当前全局状态。

后续 historical/disclosed 运行已完成 OCR/corpus/Embedding 与 pre-GT 链，但只用 AI 盲标草稿得到 Gate B provisional `12/24`，`human_review_status=pending`，没有独立人工 Ground Truth attestation，因此 official Gate B 仍未 finalize，默认配置不能切换。同一披露上下文上的 Gate C 随后真实执行并失败：`verified_v3=0/24 accepted`，其中 20/24 在模型调用前因 `no_fact_binding` 拒绝。根因修复不改写上述结果；修复后的新 sealed holdout 尚未执行。完整后续事实见 [PDF Router V3 报告](PDF_ROUTER_V3_REPORT.md)。

## 证据事故与修复

规划阶段的只读代理误运行 cache-only 命令，导致旧 candidate 文件字节 SHA 从 `179476…` 改为 `775552…`，而历史 score 仍引用旧 SHA；ranking SHA 未变化。旧文件字节无法凭空恢复，所以本报告不宣称旧 file-SHA 链完整。

已修复：candidate/score 输出存在时默认拒绝；canonical 路径即使 `--force` 也不可覆盖；canonical identity 排除 runtime、时间戳和绝对路径；scorer 在加载 Ground Truth 前验证 cache/canonical/ranking identity。尚未运行新 candidate/score 生成，因此没有再次触碰 canonical evidence。

## 真实边界

- SQLite queue 只适合单机低并发；没有证明多机扩展能力。
- 跨 SQLite 与 Chroma/文件系统仍不可能形成单一 ACID 事务；V2 通过唯一 staging、worker/attempt CAS、失败补偿清理、幂等删除和 legacy migration 控制可见性与恢复，但不等同于分布式事务。
- Paddle worker 代码与 artifact 契约已测试，V1 400 页 artifact 也验证通过；本轮没有重新执行 400 页 GPU OCR。
- 本地 E2E 使用无 API key 的确定性 mock Embedding，只证明持久流程与索引一致，不证明答案质量。
- 在线词法通道仍会扫描当前知识库候选，需在更大语料上继续测延迟。
- 默认仍为 `PDF_PADDLE_ARTIFACT_ENABLED=false`、`RETRIEVAL_PROFILE=legacy`、`RAG_ANSWER_PROFILE=legacy`、`TOP_K=3`；这是主动的质量门禁，不是遗漏。
