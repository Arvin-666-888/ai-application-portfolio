# V3 可信答案评测

该目录只承载 Router V3 的结构化答案、证据校验、拒答、延迟和成本评测，不覆盖 V1/V2 历史 artifact。

```text
[query-only]
    │ candidate 阶段，不加载 Ground Truth
    ▼
[candidate.json + ranking identity]
    │ generation 阶段，仍不加载 Ground Truth
    ▼
[generation.json]
    │ 最后由 scorer 加载独立 Ground Truth
    ▼
[score.json + finalized manifest]
```

## 门禁

- 未知 citation 和无证据数值的接受数必须为 0。
- 已接受答案的 citation validity 必须为 100%。
- 独立集 accepted-answer strict precision 目标为至少 90%，同时必须单独报告 coverage，禁止用大量拒答掩盖能力不足。
- 真实运行前必须冻结模型、Prompt、检索配置、延迟和成本阈值。
- finalized run 必须通过 `python evals/v3/validate_run.py evals/v3/runs/<run_id>`；validator 会检查三个阶段 SHA、Ground Truth 解封顺序、forbidden fields、全部 required identities、路径不得逃逸 run 目录，以及 finalized/immutable 状态。

当前已提供工程 runner 和确定性测试。2026-07-29 的 historical/disclosed holdout 已执行真实 API generation/score，但 Gate C 失败；新的独立 sealed holdout 尚未执行，因此不能声称 V3 质量验收通过。

## 当前状态与上游硬前置

2026-07-29 的 `v3-holdout-20260729-r1` 已真实执行但 Gate C failed：20/24 在模型前以 `no_fact_binding` 拒绝；该 holdout 又已被 AI 草稿解封，只能保留为 historical/disclosed-holdout 失败证据，不能调规则后重跑成 official。

修复后的 runner 不再只接受 Gate B pre-GT freeze。V3 candidate 必须验证 Gate B `score.json + final_manifest.json` 为 official/finalized/immutable/Gate B passed，并锁定同一 GT/attestation SHA。正式 GT/attestation 使用 `evals/common/` 下的 v2 schema 和模板；完整双人独立复核流程见 `docs/PDF_ROUTER_V3_HANDOFF.md`。

## Runner

在项目目录执行，四个阶段均拒绝覆盖已有 artifact；`candidate` 必须显式指定 Gate B 冻结检索 profile，`generate` 只消费该阶段复制的 contexts：

```bash
python evals/v3/run_eval.py --run-id <v3_run_id> candidate --gate-b-run <gate_b_run_dir> --retrieval-profile financial_v2
python evals/v3/run_eval.py --run-id <v3_run_id> generate
python evals/v3/run_eval.py --run-id <v3_run_id> score --ground-truth <private_ground_truth.json> --attestation <ground_truth_attestation.json>
python evals/v3/run_eval.py --run-id <v3_run_id> finalize
python evals/v3/validate_run.py evals/v3/runs/<v3_run_id>
```

真实生成前需在 `.env` 配置 `API_KEY`、`BASE_URL`、`MODEL`。若要满足正式成本门禁，还必须配置 `LLM_INPUT_COST_PER_1M` 与 `LLM_OUTPUT_COST_PER_1M`；否则 cost 会如实标记 `unavailable`，run 只能保持 provisional。预冻结的 coverage/latency/cost 阈值见 `thresholds.json`。
