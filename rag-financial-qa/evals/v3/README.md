# V3 可信答案评测

该目录只承载 Router V3 的结构化答案、证据校验、拒答、延迟和成本评测，不覆盖 V1/V2 历史 artifact。

```text
[query-only]
    │ candidate 阶段，不加载 Ground Truth
    ▼
[candidate.json + gate_b/ fixed provenance bundle]
    │ generation 阶段，仍不加载 Ground Truth
    ▼
[generation.json]
    │ scorer 从固定 evals/router_v2_holdout locator 加载 official v2 bundle
    ▼
[score.json + finalized manifest]
```

## 门禁

- 未知 citation 和无证据数值的接受数必须为 0。
- 已接受答案的 citation validity 必须为 100%。
- 独立集 accepted-answer strict precision 目标为至少 90%，同时必须单独报告 coverage，禁止用大量拒答掩盖能力不足。
- 真实运行前必须冻结模型、Prompt、检索配置、延迟和成本阈值。
- finalized run 必须通过 `python evals/v3/validate_run.py evals/v3/runs/<run_id>`；validator 会从固定 official bundle locator 重新执行完整 Ground Truth/attestation v2 contract，重建 Gate B frozen candidate provenance 和生产 citation verifier，并重新计算逐 case score、aggregate metrics 与 Gate C decision。存储的 cases/metrics/gate 必须与确定性重算结果规范 JSON 完全一致，bundle、Gate B 工件、实现源码与阶段 SHA identity 也必须逐项匹配。

当前已提供工程 runner 和确定性测试。2026-07-29 的 historical/disclosed holdout 已执行真实 API generation/score，但 Gate C 失败；新的独立 sealed holdout 尚未执行，因此不能声称 V3 质量验收通过。

## 当前状态与上游硬前置

2026-07-29 的 `v3-holdout-20260729-r1` 已真实执行但 Gate C failed：20/24 在模型前以 `no_fact_binding` 拒绝；该 holdout 又已被 AI 草稿解封，只能保留为 historical/disclosed-holdout 失败证据，不能调规则后重跑成 official。

修复后的 runner 不再只接受 Gate B pre-GT freeze。V3 candidate 必须验证 Gate B `score.json + final_manifest.json` 为 official/finalized/immutable/Gate B passed，并将 `paired_candidates.json`、`pre_gt_freeze.json`、official score 和 final manifest 复制到 run 内固定 `gate_b/` locator。score/finalize/validator 会重新验证这条 SHA 链并按冻结 retrieval profile 重建 candidate cases。正式 GT/attestation 只能从固定 `evals/router_v2_holdout/private/` locator 加载，使用 `evals/common/` 下的完整 v2 contract；完整双人独立复核流程见 `docs/PDF_ROUTER_V3_HANDOFF.md`。

## Runner

在项目目录执行，四个阶段均拒绝覆盖已有 artifact；`candidate` 必须显式指定 Gate B 冻结检索 profile，`generate` 只消费该阶段复制的 contexts：

```bash
python evals/v3/run_eval.py --run-id <v3_run_id> candidate --gate-b-run <gate_b_run_dir> --retrieval-profile financial_v2
python evals/v3/run_eval.py --run-id <v3_run_id> generate
python evals/v3/run_eval.py --run-id <v3_run_id> score
python evals/v3/run_eval.py --run-id <v3_run_id> finalize
python evals/v3/validate_run.py evals/v3/runs/<v3_run_id>
```

真实生成前需在 `.env` 配置 `API_KEY`、`BASE_URL`、`MODEL`。若要满足正式成本门禁，还必须配置 `LLM_INPUT_COST_PER_1M` 与 `LLM_OUTPUT_COST_PER_1M`；generation 会冻结 pricing snapshot，score/validator 只从 token usage 与该 snapshot 重算 cost，不信任 output 自报的 `estimated_cost`。若费率或 token usage 不完整，cost 会如实标记 `unavailable`，Gate C 不能通过。预冻结的 coverage/latency/cost 阈值见 `thresholds.json`。

## 证据边界

本地 manifest 与 source descriptor 能证明 artifact、固定 locator、Gate B 副本和验证时当前源码字节一致，并检测生成后的局部漂移；它们不是数字签名，也不能抵抗有权在评分前整组改写源码和全部 evidence 的攻击者。若需要证明原始 Gate B release、generation 时序或墙钟 latency 的外部真实性，必须额外使用受保护 Git commit、签名 CI attestation 或对象锁/append-only 存储。当前 latency gate 使用 generation 阶段记录的 `latency_ms`，不把它描述成可信时间戳。
