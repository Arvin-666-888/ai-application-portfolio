# PDF Router V3 设计：可信答案与原型级生产治理

## 1. 目标与边界

V3 不改写 L1/L2/L3 解析链，也不把当前 SQLite/Chroma 原型包装成企业生产平台。它解决的是：**检索到证据后，候选答案是否有资格向用户发布。**

```text
[问题 + 当前文档精确版本]
    │ 检索
    ▼
[legacy / financial_v2 排序]
    │ C1..Cn
    ▼
[Citation Ledger]
    │ 生成前证据绑定检查
    ▼
[结构化候选答案]
    │ Decimal + 同行/同句 verifier
    ├──── 失败 ───► [确定性拒答]
    ▼
[Verified Answer + RagRun 审计]
```

工程原则：

- 模型负责提出候选答案，确定性代码决定是否发布；
- `RAG_ANSWER_PROFILE=verified_v3` 与 `RETRIEVAL_PROFILE=financial_v2` 独立开关；
- V2 holdout 未通过前不切 L3/financial_v2 默认；V3 真实答案评测未通过前不切 verified_v3 默认；
- 复杂公式、跨表计算和通用语义蕴含不在本轮范围；
- 多租户、RBAC、SSO、分布式队列、不可篡改审计和正式 SLO 不在本轮范围。

## 2. 可信答案契约

`ChatResponse` 保留原有 `answer` 与 `sources`，新增：

- `answer_status`：`verified / refused / unverified / failed`；
- `structured_answer`：指标、原始数值、单位、币种、年度、公司、口径和 citation IDs；
- `verification`：是否通过、错误码和已验证引用；
- `run`：trace、模型、profile、耗时、token 和估算成本。

每条 source 获得请求内稳定 ID `C1..Cn`。完整证据只保存在服务端 Citation Ledger；API 只返回截断 snippet。

## 3. Verifier

使用 `Decimal` 处理：

- 千分位、全角数字、括号负数；
- `%`、百分点、bp/bps；
- 元、千元、万元、百万元、亿元；
- CNY/RMB/人民币、USD/美元、HKD/港币。

数值问题生成前必须在同一证据片段中找到可绑定的指标、显式单位数值，以及问题指定的年度、公司和口径。生成后再次校验：

- citation 必须存在；
- 每个数值 fact 必须有 citation；
- 指标和值必须在同一表格逻辑行或正文同一句；
- 单位、币种、年度、公司、指标和口径不得冲突；
- 不允许跨无关片段拼字段；
- 回答中的额外数值必须有对应 structured fact；
- 多值歧义、复杂公式和 schema 错误 fail closed。

任何失败都替换为稳定拒答，未经验证的原答案不会保存或返回。

## 4. 同步与 SSE

`verified_v3` 的同步与 SSE 共用 `execute_answer()`。V3 SSE 不透传上游 token，而是等待结构化候选完成验证后发送：

```text
meta -> content -> sources -> result -> [DONE]
```

这是可信优先的取舍：time-to-first-content 增加，但客户端不会先收到无法撤回的幻觉数值。legacy `/stream` 继续保留增量 content 事件以兼容旧客户端；两种流式路径都使用独立短生命周期 Session 完成消息与运行审计，不携带 request-scoped DB Session。

## 5. 索引与摄取一致性

- 查询使用精确 `(doc_id, active_index_version)` pair，而不是共享版本名集合；
- 新索引 CAS 发布后 best-effort 清理同文档旧版本；清理失败不回滚发布，查询隔离仍保证旧版本不可见；
- `cleanup_stale_indexes.py` 默认只列出，只有 `--apply` 才删除；
- `ParseRuntimeOptions` 作为 job-scoped 不可变配置传入解析链，ingest/finalize 不再修改全局 Paddle settings。

## 6. RagRun 与安全治理

每次问答持久化一条 `RagRun`：

- trace、用户、KB、对话与 message IDs；
- answer/retrieval profile、模型和精确 doc-version targets；
- answered/refused/failed 状态与 reason codes；
- retrieval/generation/verification/persistence/total 耗时；
- provider 返回的 token；usage 缺失时保持 null，不能记 0 冒充；
- 可选费率计算的 estimated cost；未配置时为 unavailable；
- question SHA 和 prompt config SHA，不重复保存完整 prompt/context。

安全收口：

- conversation、KB、RagRun 均做用户所有权检查；
- evidence 明确为不可信数据，文档中的指令不进入真实 role；
- 非 DEBUG 禁止默认 SECRET_KEY；CORS 使用 allowlist；
- 密码依赖缺失时 fail closed，不退化为普通 SHA-256；
- Job API 不返回服务器 artifact 绝对路径或原始异常；
- HTTP/SSE 对外使用稳定错误码和 trace ID。

## 7. 发布门禁

- Gate A：V3 工程测试、迁移、mock/API E2E、证据门禁通过；这只证明工程合同，不证明真实答案质量；
- Gate B：V2 独立 sealed holdout 达到预注册 Recall 门槛并取得真实独立人工 Ground Truth attestation 后，才可切 financial_v2/L3 默认；
- Gate C：在 official-finalized Gate B 的同一受控证据合同上，独立真实 API 评测达到 precision、coverage、citation、unsupported acceptance、延迟和成本门槛后，才可切 verified_v3 默认。

当前 historical/disclosed Gate B 只有 AI 盲标草稿上的 provisional `12/24`，无独立人工 attestation；同一披露上下文的 Gate C 已真实执行但失败，`verified_v3=0/24 accepted`。根因修复后的新 sealed holdout 尚未执行。因此默认继续为 `PDF_PADDLE_ARTIFACT_ENABLED=false`、`RETRIEVAL_PROFILE=legacy`、`RAG_ANSWER_PROFILE=legacy`、`TOP_K=3`。

Gate B/C 失败时保留 opt-in 工程能力并如实报告，不针对已披露数据逐题调参后冒充独立验收。
