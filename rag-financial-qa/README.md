# 上市公司财报与公告智能问答系统

基于 RAG（检索增强生成）的金融文档问答原型，使用 FastAPI + ChromaDB + OpenAI-compatible API 实现财报、公告、研报摘要等文档的上传、切分、向量检索、引用溯源、多轮问答、SSE 流式输出和资料外拒答。

> 当前项目用于验证金融文档 RAG 的核心后端链路，包括文档入库、向量检索、来源返回、拒答控制和评测。项目尚未按生产级投研平台要求建设。

## 项目亮点

- **RAG 全链路**：文档解析 -> 文本切分 -> Embedding -> ChromaDB -> 检索 -> Prompt -> 回答生成。
- **金融场景化**：围绕财报摘要、经营风险、收入结构、管理层展望等问题组织问答。
- **混合检索雏形**：向量候选召回后，结合中文关键词重叠分数做轻量重排，降低纯向量误召回风险。
- **来源可追溯**：回答返回来源文档、相关片段和相关度，便于核验。
- **资料外拒答**：资料不足时拒答；对股价预测、买卖建议等金融高风险问题增加应用层护栏。
- **后端工程完整性**：FastAPI 分层架构、JWT 鉴权、SQLite 元数据、文档失败原因记录、SSE 流式输出、Docker 启动配置。
- **可评测**：内置 JSONL 评测集和脚本，输出检索命中率、引用支撑率、拒答准确率、关键词命中率。
- **可演示**：支持无 API Key 的 mock 模式，便于本地学习和接口联调。

## 快速启动

### 1. 安装依赖

在本项目根目录（即 `rag-financial-qa/`）执行：

```bash
pip install -r requirements.txt
```

开发和测试环境：

```bash
pip install -r requirements-dev.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，按需填写 API 配置：

```env
API_KEY=your-api-key
BASE_URL=https://api.openai.com/v1
MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
```

不配置 `API_KEY` 也可以运行，系统会使用 mock mode（确定性随机向量 + 模拟回答），适合检查接口和流程。

### 3. 启动服务

本地 Quickstart 必须同时运行 API 和普通文档 worker；否则上传只会进入持久队列并停留在 `queued`，不会变为 `ready`。

终端 1：

```bash
python scripts/migrate_router_v2.py --apply
uvicorn app.main:app --reload --port 8000
```

终端 2：

```bash
python -m app.workers.document_worker
```

访问：

```text
http://localhost:8000/docs
```

### 4. Docker 启动

```bash
copy .env.example .env
docker compose up --build
```

Docker Compose 会同时启动 API 和普通文档 worker。Docker 模式下，SQLite 数据库保存在项目根目录的 `data/`，上传文件保存在 `uploads/`，向量数据保存在 `chroma_data/`。

## 最小演示流程

1. 注册/登录，获取 Token。
2. 创建知识库，例如“某上市公司 2024 财报知识库”。
3. 上传 `evals/fixtures` 里的金融样例文档。
4. 等待文档状态变为 `ready`。
5. 创建对话。
6. 提问资料内问题，查看回答和来源引用。
7. 提问资料外问题或股价预测问题，验证拒答。

建议演示问题：

- 公司 2024 年营业收入是多少？
- 2024 年毛利率是多少，为什么提升？
- 云资源价格上升会带来什么风险？
- 企业知识库系统有哪些数据安全风险？
- 请预测公司明年股价会涨到多少？

## 自动评测

先上传 `evals/fixtures` 下的样例文档到同一个知识库，再运行：

```bash
python evals/run_eval.py --kb-id 1 --top-k 3
```

只评测检索，不调用大模型生成：

```bash
python evals/run_eval.py --kb-id 1 --top-k 3 --retrieval-only
```

先检查评测集结构，不需要启动服务或创建知识库：

```bash
python evals/run_eval.py --validate-only
```

评测集包含 24 条功能验收问题。每条 case 包含 `category`、`difficulty`、`answer_type`、`expected_sources`、`expected_keywords`、`expected_context_keywords` 和 `should_refuse`，用于区分资料内事实、原因解释、风险问题、跨文档综合、资料外问题和金融高风险拒答。

评测指标：

| 指标 | 说明 |
|---|---|
| `retrieval_hit_rate@k` | Top-K 检索结果是否命中期望来源或期望关键词 |
| `source_support_rate` | 返回给用户的 sources 是否能支撑答案 |
| `refusal_accuracy` | 资料外问题和投资建议类问题是否被拒答 |
| `answer_keyword_match_rate` | 资料内答案是否覆盖关键事实 |

## API 接口

### 认证

- `POST /api/auth/register` - 注册
- `POST /api/auth/login` - 登录
- `GET /api/auth/me` - 获取当前用户

### 知识库

- `GET /api/knowledge-bases` - 知识库列表
- `POST /api/knowledge-bases` - 创建知识库
- `DELETE /api/knowledge-bases/{id}` - 删除知识库

### 文档

- `POST /api/documents/upload?kb_id={id}` - 上传文档
- `GET /api/documents?kb_id={id}` - 文档列表
- `DELETE /api/documents/{id}` - 删除文档

### 问答

- `POST /api/chat/conversations` - 创建对话
- `GET /api/chat/conversations` - 对话列表
- `GET /api/chat/conversations/{id}/messages` - 对话消息
- `POST /api/chat/{conversation_id}` - 问答（同步）
- `POST /api/chat/{conversation_id}/stream` - 问答（流式 SSE）

## 项目结构

```text
app/
├── main.py                 # 应用入口，中间件，异常处理
├── config.py               # 配置管理
├── database.py             # 数据库连接和 Session 管理
├── models/models.py        # SQLAlchemy ORM 模型
├── schemas/schemas.py      # Pydantic 请求/响应模型
├── routers/                # API 路由
├── services/               # 业务逻辑
└── utils/
    ├── retrieval.py        # 关键词召回、混合重排、金融拒答护栏
    ├── text_splitter.py    # 文本分块
    └── vector_store.py     # ChromaDB 封装

docs/
├── DEMO_RUNBOOK.md           # 本地演示与验证流程
└── EVALUATION_REPORT_TEMPLATE.md  # 评测报告模板

evals/
├── fixtures/               # 样例金融文档
├── questions.jsonl         # 评测问题集
└── run_eval.py             # 评测脚本
```

## 核心技术点

| 技术点 | 实现方式 |
|---|---|
| 文档解析 | TXT/MD 直接读取；PDF 默认用 Unstructured `partition_pdf(strategy="fast")` 跑通流程，可通过 `use_hi_res=True` 启用结构化表格提取 |
| 表格索引 | `Table.metadata.text_as_html` 转 Markdown，按完整行切块并保留来源、页码、块 ID 与有界 HTML/Markdown metadata |
| 文本分块 | 递归字符分块，默认 `chunk_size=400`、`overlap=80` |
| 向量化 | OpenAI-compatible Embedding API，支持 mock mode |
| 向量存储 | ChromaDB PersistentClient，按知识库隔离 collection |
| 检索 | 向量候选召回 + 中文关键词重叠分数重排 |
| 回答生成 | 检索结果拼接 Prompt 后调用大模型 |
| 来源引用 | 返回来源文档、相关片段和相关度 |
| 拒答 | Prompt 约束 + 相似度过滤 + 金融高风险问题护栏 |
| 失败诊断 | 文档处理失败时记录 `error_message` |
| 多轮对话 | 滑动窗口保留最近历史 |
| 流式响应 | SSE Server-Sent Events |
| 认证 | JWT Token + Bearer 认证 |

## 测试

```bash
pytest
```

当前测试聚焦在文本切分、关键词召回、混合重排和金融拒答护栏。

本地总体验证：

```bash
python scripts/pre_interview_check.py
```

该脚本会检查关键文件、依赖安装状态、语法、pytest 和评测问题数量。
如果只是做静态材料检查、尚未安装运行依赖，可以临时加 `--allow-missing-deps`；完整验证前应安装依赖并让检查通过。

## 端到端演示验收

启动服务后运行：

```bash
python scripts/demo_e2e.py --base-url http://127.0.0.1:8000
```

脚本会自动完成注册、登录、创建知识库、上传样例文档、等待 ready、创建对话、资料内问答和股价预测拒答检查。建议先跑这个脚本，再手工打开 Swagger 复核接口。

## PDF Router V3：可信答案与原型级治理

V3 已完成工程实现：`verified_v3` 通过结构化答案、请求内 `C1..Cn` Citation Ledger、Decimal 数值/单位/币种/年度/公司/指标/口径校验和 fail-closed 拒答，决定候选答案是否允许发布。`verified_v3` 同步与 SSE 共用可信决策，legacy SSE 保留增量 content 事件；`RagRun` 记录 trace、精确 doc-version targets、阶段耗时、token、可选估算成本和拒答原因。

```text
[检索结果]
    │ Citation Ledger + 生成前证据绑定
    ▼
[结构化候选答案]
    │ 确定性 verifier
    ├── 失败 ──► [拒答]
    ▼
[Verified Answer + RagRun]
```

当前 Router V2/V3 的一次 historical/disclosed holdout 运行已经完成，但没有形成可发布的独立验收结论：Gate B 只有 AI 盲标草稿上的 provisional `12/24`，没有独立人工 Ground Truth attestation；Gate C 使用该披露上下文真实执行并失败，`verified_v3=0/24 accepted`，其中 20/24 在模型调用前因 `no_fact_binding` 拒绝。根因修复已加入 table semantic context、明确单位/列绑定、query intent 修正、Ground Truth/attestation v2 exact-SHA 合同，以及 Gate B official-finalized 才能进入 Gate C 的硬前置；这些修复不改写旧失败，也不等于新的独立质量验收。新的 sealed holdout 尚未执行，因此默认继续保持 `PDF_PADDLE_ARTIFACT_ENABLED=false`、`RETRIEVAL_PROFILE=legacy`、`RAG_ANSWER_PROFILE=legacy`、`TOP_K=3`。详见 [V3 设计](docs/PDF_ROUTER_V3_DESIGN.md) 和 [V3 报告](docs/PDF_ROUTER_V3_REPORT.md)。

## PDF Router V2：持久化摄取基建

V2 已把上传后的长时处理从 Web 进程内 `asyncio.create_task` 拆为 SQLite 持久队列和两个独立 CLI worker：

```text
[上传 API]
    │ 保存文件/SHA + enqueue ingest
    ▼
[SQLite document_jobs]
    ├──► [document worker: L1/L2 snapshot + finalize/index]
    └──► [Paddle worker: page OCR artifact]
                         │ validated L3
                         ▼
              [版本化 Chroma upsert]
```

本地单机原型支持幂等 enqueue、条件 claim、lease/heartbeat、有限重试、stale recovery、失败任务人工 requeue、解析审计和版本化索引。SQLite 适合本机低并发演示，不代表多机生产队列；未来可在保持 repository/service 状态机契约的前提下替换为 PostgreSQL 或 Redis 队列。

```powershell
python scripts/migrate_router_v2.py --apply
python -m app.workers.document_worker
# 在独立 PaddleOCR venv 中启动：
python -m app.workers.paddle_worker
```

新公司/新年度的第一组 historical/disclosed holdout 已执行 pre-GT 链，但 Gate B 只有缺少独立人工 attestation 的 provisional `12/24`，不能 finalize 或切换默认配置；同一披露上下文上的 Gate C 真实执行并失败，`verified_v3=0/24 accepted`。修复后的新 sealed holdout 尚未执行，因此默认仍保持 `PDF_PADDLE_ARTIFACT_ENABLED=false`、`RETRIEVAL_PROFILE=legacy`、`RAG_ANSWER_PROFILE=legacy`；不能把“V2 基建完成”或 provisional 指标表述为“V2 质量验收通过”。详见 [PDF Router V2 报告](docs/PDF_ROUTER_V2_REPORT.md) 和 [PDF Router V3 报告](docs/PDF_ROUTER_V3_REPORT.md)。

## PDF Router V1 与真实评测证据

项目已构建并验证三层金融文档智能问答中台原型。这里的“验证”是原型级技术验证，不代表生产级、业务达标，也不表述为“已落地 2026 主流方案”。

```text
[上传的金融 PDF]
    │ 1. 逐物理页抽取正文与确定性特征
    ▼
[L1：pdfplumber 物理页正文保底，保留全部可用页]
    │ 2. 仅候选页进入表格增强
    ▼
[L2：在线 Unstructured hi_res 候选页解析]
    │ 3. L2 无有效表格或失败时读取离线产物
    ▼
[L3：在线只消费 validated Paddle artifact]
    │ 4. L1 正文 + 选中的表格块进入索引
    ▼
[向量/词法检索与引用返回]
```

L2 的在线路由与回退行为由自动测试覆盖。L3 的 PaddleOCR/PP-StructureV3 在独立离线环境生成可校验 artifact；FastAPI 进程不加载 Paddle/GPU runtime。当前离线真实 corpus 是 **L1 + 已验证 L3 artifact 的投影**，没有离线重跑 L2，不能把它写成 “Unstructured hi_res 离线全量复跑”。生产应用代码不加载 ground truth，也没有 Paddle runtime import 或固定 5 报告、30 题、4,125/1,167 chunks 等数据集常量。

完整架构、数据、哈希与边界见 [PDF Router V1 报告](docs/PDF_ROUTER_V1_REPORT.md)；后续 V2/V3 的实现与验收边界分别见 [V2 设计](docs/PDF_ROUTER_V2_DESIGN.md)、[V2 报告](docs/PDF_ROUTER_V2_REPORT.md)、[V3 设计](docs/PDF_ROUTER_V3_DESIGN.md) 和 [V3 报告](docs/PDF_ROUTER_V3_REPORT.md)。

### 中文 A 股 2024 年报：主验收证据

真实数据为格力电器、美的集团、贵州茅台、比亚迪、招商银行 5 份 2024 年中文 A 股年报。L1 覆盖全部 **1,338 个 PDF 物理页**；无 ground truth 的规则从候选池中选择 400 页，离线 OCR artifact 为 **400/400 完成、0 missing、0 invalid/drop**，其中 305 页有表、95 页合法无表，共 601 张表、1,167 个 L3 chunks。严格表格证据覆盖为 **26/30**。

新的 frozen corpus 使用 **4,125 个 L1 chunks + 1,167 个 L3 chunks = 5,292 chunks**。其 `degraded` 状态来自 95 个“已完成但无表”候选页以及 1 条 legacy candidate policy identity 兼容记录，是可解释、可审计的降级，不是 artifact 缺失或损坏。

| 中文历史阶段 | Row-aware Recall@5 | 说明 |
|---|---:|---|
| Unstructured 全量旧臂 | **5/30（16.67%）** | `pdfplumber` 基线 |
| Unstructured 全量新臂 | **0/30（0%）** | hi_res 结构化臂，`parse_failure_count=0`，真实失败证据保留 |
| Paddle legacy row-aware | **7/30（23.33%）** | L1 + Paddle 表格，legacy 排序 |
| `router_v1 + financial_v2` | **14/30（46.67%）** | MRR `0.251111`，Candidate Recall@50 `23/30` |

最新 30 题已经参与 smoke、coverage 和方案开发，属于 **development set**，不是 holdout；`financial_v2` 仍非默认检索配置。因此 46.67% 只能证明当前原型相对历史链路有技术改善，不能声明业务达标。历史 `5/30 -> 0/30` 的失败没有被新结果覆盖或删除。

### 五份外文年度报告：补充历史证据

Alphabet、Amazon、Apple、Microsoft、NVIDIA 的 5 份官方年度报告/10-K（462 个 PDF 物理页、15 条固定 ground truth）曾完成真实 API 全量评测。严格命中要求同一个 Top-5 chunk 同时匹配报告 basename、PDF 物理页码、指标和数值边界：旧版 **3/15（20.00%）**，Unstructured hi_res 新版 **4/15（26.67%）**，`parse_failure_count=0`，完整运行约 46 分钟。它是补充证据，不能替代中文 A 股主验收，也不能被中文新结果覆盖。

正式仓库纳入并可公开引用的证据是 [PDF Router V1 报告](docs/PDF_ROUTER_V1_REPORT.md)、[检索 V2 开发集报告](docs/RETRIEVAL_V2_REPORT.md)、[历史任务 2 验收报告](docs/TASK2_ACCEPTANCE_REPORT.md)、[PDF Router V2 报告](docs/PDF_ROUTER_V2_REPORT.md) 和 [PDF Router V3 报告](docs/PDF_ROUTER_V3_REPORT.md)。历史运行时曾生成 `compare_result*.json`、`evals/task2_paddleocr/reports/*.json` 与 `evals/v3/runs/*` 等本地 artifacts；它们未纳入本次正式提交白名单，因此这里只作为报告中记录了身份、指标与边界的历史本地证据，不提供仓库链接，也不要求读者依赖这些被排除文件。

冻结身份：ranking `2711483dc023251a1e197371633a972aa3162abb7f0ec876bbf1c4a10c4588e6`；最终 v2 corpus `3fdfa19ac4d54ccfa056fc45e074c70c03c617ef6087fe2c1b1948b28c60661e`；candidate `179476d9a411af97daa656ebbd60d9d71d01d97255474e39ab2c1c577aaf8187`；score `6305ffd5668ad51972918a596d1e3df265d46094422fa231fb0a7e90f4fabcca`；config `7b386c26a3ba8e75c845dc9074f7ce49aea15a5fc287b3d4306d3b9323466a31`；candidate identity `92fd6ef6ee7c2d46355e3169c58913a6f089166096446936bdb1ae90a362cdda`；policy fingerprint `839996581d15ade3592ccd72014a911a71d545a9a993b8daafee828e703eeccb`。

独立审查后的修复还包括：loader 强制 routed L1 canonical identity 等于 baseline，并拒绝旧 9,488-chunk corpus；builder v2 分别写入 L1/L3 layer SHA；L3 默认关闭，启用时必须 pin engine fingerprint；artifact metadata 不泄露绝对路径；未尝试 L2 时不再错误记录 `fallback_from=L2`；JSON 仅使用一个同目录临时文件做原子替换。

5 个评测脚本统一共享 `atomic_json`；Windows 同目标 8 线程、每线程 64 次写入，连续 3 轮测试均为 0 error，且无 `.tmp` 残留。

### 精确复现：冻结 corpus、无泄漏检索与评分

以下命令只做只读验证。任何需要重新生成的 artifact 都必须写入新的 scratch/run 路径；历史 `*_v2.json` 和 compare 文件已设为不可覆盖。

```bash
python scripts/07_build_routed_corpus.py --validate-only
python evals/router_v2_holdout/validate_freeze.py --require-pdfs
# 若已配置独立 PaddleOCR venv，则用该环境执行：
python scripts/03_run_paddleocr_tables.py --validate-only
python -m pytest tests -q -p no:cacheprovider --basetemp "${TMPDIR:-/tmp}/rag-pytest"
```

如需重新运行候选检索或评分，必须把输出写到项目目录之外的 scratch 目录或明确的新文件名；不要覆盖报告中记录的历史 canonical artifacts。

当前测试状态以本次 fresh `pytest` 终端输出为准，不在文档中硬编码可能随实现变化的用例总数。历史阶段的固定测试数仅保留在对应版本报告中，并明确标注当时阶段；它们不代表当前提交。依赖完整性同样以本地 fresh `python -m pip check` 输出为准。

## 局限与三版演进边界

- **V1**：三层路由、L1 全页保底、validated L3 artifact、可审计降级和防泄漏冻结评分；代价是离线 OCR，开发集结果不等于独立验收。
- **V2**：持久 OCR worker/queue、幂等恢复和版本化索引发布；代价是状态机、双环境和运维复杂度。独立 holdout 仍未解封评分，所以默认链路不切换。
- **V3**：结构化候选答案、确定性数值/单位/年度/引用校验、证据不足拒答和 RagRun 治理已完成工程验证；代价是更高首包延迟、严格拒答降低 coverage，以及持续规则/评测成本。独立真实模型质量仍待验收。

三版到此总结，不继续制造 V4/V5。当前还存在 Markdown 合并单元格降级、跨页表格不主动拼接，以及 SQLite/本地 ChromaDB 尚未替换为生产基础设施等限制。

## LangChain 对照 demo

主项目保留手写 RAG 链路，`examples/` 中额外提供 LangChain 对照实现：

```bash
pip install -r requirements.txt
pip install -r requirements-langchain.txt
python examples/langchain_rag_demo.py --mock --question "2024年公司营业收入是多少？"
python examples/langchain_rag_demo.py --mock --question "竞争对手A公司收入是多少？"
```

该 demo 使用 `Document`、`RecursiveCharacterTextSplitter`、Chroma retriever 和可选 `OpenAIEmbeddings/ChatOpenAI`，输出 `answer`、`sources`、`snippet` 和 `relevance`，用于和主项目的手写 RAG 实现进行对比。
