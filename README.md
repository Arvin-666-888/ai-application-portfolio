# AI Application Portfolio: RAG + Agent

这是一个面向 AI 应用开发岗位的作品集仓库，包含两个可本地运行的后端项目：

| 项目 | 方向 | 主要能力 |
|---|---|---|
| `rag-financial-qa` | 金融文档 RAG 问答 | 文档上传、文本切分、Embedding、ChromaDB 检索、来源引用、域外拒答、JSONL 评测 |
| `business-data-agent` | 经营数据分析 Agent | Function Calling、工具 schema、自然语言转 SQL、只读 SQL 安全、工具调用轨迹、图表和报告导出 |

> 当前仓库定位为求职展示型原型，重点展示 RAG、Agent 和 AI 应用后端工程链路，不夸大为生产级平台。

## 技术栈

- Python / FastAPI / SQLAlchemy / Pydantic / SQLite
- ChromaDB / Embedding / RAG / 来源引用 / 域外拒答
- OpenAI-compatible API / Function Calling / Agent 工具调用
- pytest / JSONL evals / Docker / Swagger / SSE

## 快速开始

### 1. RAG 金融文档问答

```powershell
cd rag-financial-qa
pip install -r requirements.txt
pytest
python evals/run_eval.py --validate-only
uvicorn app.main:app --reload --port 8000
```

打开 Swagger：`http://127.0.0.1:8000/docs`

无 API Key 时可以使用 mock mode 跑通接口链路。真实模型效果需要配置 `.env`：

```powershell
copy .env.example .env
```

### 2. 经营数据分析 Agent

```powershell
cd business-data-agent
pip install -r requirements.txt
pytest
python evals/run_agent_eval.py
python scripts/smoke_demo.py
uvicorn app.main:app --reload --port 8001
```

打开 Swagger：`http://127.0.0.1:8001/docs`

无 API Key 时默认使用 mock mode，可演示注册、数据源、自然语言分析、SQL 查询、工具轨迹和报告导出。

## 演示问题

### RAG 项目

- 公司 2024 年营业收入是多少？
- 2024 年毛利率是多少，为什么提升？
- 云资源价格上升会带来什么风险？
- 请预测公司明年股价会涨到多少？

### Agent 项目

- 2024 年每月收入趋势如何？
- 各产品线毛利率是多少？
- 收入贡献最高的前 5 个客户是谁？
- 哪些月份净现金流为负？


## LangChain 对照实现

两个主项目仍然保留手写实现，同时补充第二版 LangChain demo，用于展示对主流框架抽象的理解：

```powershell
cd rag-financial-qa
pip install -r requirements.txt
pip install -r requirements-langchain.txt
python examples/langchain_rag_demo.py --mock --question "2024年公司营业收入是多少？"

cd ..\business-data-agent
pip install -r requirements.txt
pip install -r requirements-langchain.txt
python examples/langchain_sql_agent_demo.py --mock --question "2024年每月收入趋势如何？"
```

说明文档见：`docs/LANGCHAIN_COMPARISON.md`。

## 评测与测试

- RAG：`evals/questions.jsonl` 覆盖资料内事实、原因解释、风险问题、资料外问题和金融高风险拒答。
- Agent：`evals/agent_questions.jsonl` 覆盖工具选择、SQL 结构、结果行数和危险 SQL 拦截。
- 两个项目均包含 pytest 测试，覆盖核心逻辑和安全边界。

## 本地验证记录

- `rag-financial-qa`：已在 mock mode 下验证，`pytest` 通过 16 个测试，`evals/run_eval.py --validate-only` 通过 24 条评测集结构校验；LangChain 对照 demo 可离线运行。
- `business-data-agent`：已在 mock mode 下验证，`pytest` 通过 24 个测试，`evals/run_agent_eval.py` 通过 7/7 条评测，LangChain SQL Agent 对照 demo 可离线运行。

## 仓库安全说明

本仓库不提交 `.env`、API Key、本地运行数据库、上传文件、向量库数据、虚拟环境、缓存和日志。示例数据仅用于本地演示和评测。
