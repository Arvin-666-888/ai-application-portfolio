# PDF Router V2 设计：持久化 OCR 与版本化索引

## 目标与边界

V2 解决 Router V1 的工程可靠性缺口：上传任务不能随 API 进程退出而丢失，OCR 要有幂等键、租约、心跳、有限重试和断点恢复，artifact 到索引的重放不能产生重复有效 chunks。V2 不解决 Rerank、结构化答案和数值/引用一致性校验，这些仍属于 V3。

## 数据流

```text
[Upload API]
    │ 保存文件、SHA、Document + ingest job（同一事务）
    ▼
[SQLite document_jobs]
    │ claim / lease / heartbeat
    ├────────► [document_worker]
    │              │ L1/L2 parse snapshot + page OCR jobs
    │              │ finalize + versioned Chroma upsert
    └────────► [paddle_worker（独立 venv）]
                   │ single-page atomic artifact
                   ▼
           [PaddleArtifactAdapter validation]
                   │ PDF/page/engine/schema/table digest
                   ▼
           [active_index_version 查询隔离]
```

## 状态与幂等

- Job：`queued -> running -> completed`；可重试错误回到 `queued` 并指数退避；超预算进入 `failed`；租约超时进入 `stale`，可重新 claim；删除文档进入 `cancelled`。
- Page OCR 幂等键：`pdf_sha256 + physical_page_number + engine_fingerprint + artifact_schema_version`。
- Document ingest/finalize 幂等键：`document_id + job_type + file/profile/index version`。
- Chroma ID：显式 V2 写入采用 `document + index_version digest + chunk_index`，使用 `upsert`；legacy 未显式传版本的调用保持旧 ID 兼容。
- 查询由数据库中的 ready document `active_index_version` 过滤；exact dense diagnostics 只保留给离线评测，在线 `financial_v2` 直接从 Chroma 按 table/text 通道取 dense candidate。

## SQLite 边界

SQLite 配置 WAL、foreign keys 和 busy timeout。claim 只在短事务中用条件更新竞争，OCR、解析、Embedding 和文件 I/O 都在事务外。该方案用于单机低并发作品集原型；多机部署应替换 repository/queue 后端，而不是把 SQLite 网络共享盘当生产消息队列。

## Artifact 边界

FastAPI 和 document worker 不 import Paddle。Paddle worker 在独立锁定环境中初始化 PP-StructureV3，只处理一条 page job，原子写与 V1 adapter 兼容的 `paddleocr-table-page-v1` artifact。finalize 再通过 adapter 校验 PDF SHA、物理页、engine fingerprint、schema、table digest；失败页保留 L1 并标记 `degraded_ready`。

## Holdout 与默认切换

原始 `evals/router_v2_holdout/` 在答案解封前冻结 4 份官方 PDF、24 条 query-only 和门槛。正式评分设计要求 legacy vs `financial_v2` 配对运行：总体 Recall@5 >= 50%，新公司与新年度各 >= 40%，且不低于 legacy。

后续 historical/disclosed 运行只在缺少独立人工 attestation 的 AI 盲标草稿上得到 Gate B provisional `12/24`，不能 official finalize；同一披露上下文上的 Gate C 真实执行并失败，`verified_v3=0/24 accepted`。根因修复后的新 sealed holdout 尚未执行。门槛正式通过前，默认保持 `PDF_PADDLE_ARTIFACT_ENABLED=false`、`RETRIEVAL_PROFILE=legacy`、`RAG_ANSWER_PROFILE=legacy` 和 `TOP_K=3`。
