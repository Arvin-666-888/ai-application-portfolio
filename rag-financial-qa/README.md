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

```bash
cd demo
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

```bash
uvicorn app.main:app --reload --port 8000
```

访问：

```text
http://localhost:8000/docs
```

### 4. Docker 启动

```bash
cd demo
copy .env.example .env
docker compose up --build
```

Docker 模式下，SQLite 数据库会保存在 `demo/data/`，上传文件保存在 `demo/uploads/`，向量数据保存在 `demo/chroma_data/`。

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
| 文档解析 | TXT/MD 直接读取，PDF 用 pdfplumber |
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
cd demo
pytest
```

当前测试聚焦在文本切分、关键词召回、混合重排和金融拒答护栏。

本地总体验证：

```bash
cd demo
python scripts/pre_interview_check.py
```

该脚本会检查关键文件、依赖安装状态、语法、pytest 和评测问题数量。
如果只是做静态材料检查、尚未安装运行依赖，可以临时加 `--allow-missing-deps`；完整验证前应安装依赖并让检查通过。

## 端到端演示验收

启动服务后运行：

```bash
cd demo
python scripts/demo_e2e.py --base-url http://127.0.0.1:8000
```

脚本会自动完成注册、登录、创建知识库、上传样例文档、等待 ready、创建对话、资料内问答和股价预测拒答检查。建议先跑这个脚本，再手工打开 Swagger 复核接口。

## 局限与后续优化

- 当前 PDF 解析以文本提取为主，复杂财报表格、跨页表格和图表理解还需要增强。
- 混合检索使用轻量关键词重排，后续可以接入 BM25、bge-reranker 或 cross-encoder reranker。
- 当前评测集是 24 条小型功能验收集，已覆盖资料内事实、原因解释、风险问题、跨文档综合、资料外问题和金融高风险拒答；后续如要更接近生产评测，可扩展到 30-50 条并保存一次真实模型评测报告。
- 当前使用 SQLite 和本地 ChromaDB，生产环境应替换为 PostgreSQL、对象存储、独立向量数据库和任务队列。

## LangChain 对照 demo

主项目保留手写 RAG 链路，`examples/` 中额外提供 LangChain 对照实现：

```bash
pip install -r requirements.txt
pip install -r requirements-langchain.txt
python examples/langchain_rag_demo.py --mock --question "2024年公司营业收入是多少？"
python examples/langchain_rag_demo.py --mock --question "竞争对手A公司收入是多少？"
```

该 demo 使用 `Document`、`RecursiveCharacterTextSplitter`、Chroma retriever 和可选 `OpenAIEmbeddings/ChatOpenAI`，输出 `answer`、`sources`、`snippet` 和 `relevance`，用于和主项目的手写 RAG 实现进行对比。
