# AI Application Portfolio: RAG + Agent

这是一个 AI 应用后端项目集合，包含三个可本地运行和验证的项目：

| 项目 | 方向 | 主要能力 |
|---|---|---|
| `rag-financial-qa` | 金融文档 RAG 问答 | 文档上传、文本切分、Embedding、ChromaDB 检索、来源引用、域外拒答、JSONL 评测 |
| `business-data-agent` | 经营数据分析 Agent | Function Calling、工具 schema、自然语言转 SQL、只读 SQL 安全、工具调用轨迹、图表和报告导出 |
| `ecommerce-multi-agent-support` | 跨境电商多 Agent 客服 | LangGraph Supervisor、商品/订单/售后分工、JWT 归属校验、敏感动作待审批、Tool Trace、离线评测 |

> 当前仓库用于验证 RAG、Agent 和 AI 应用后端工程链路。项目以本地可复现和核心流程完整为目标，尚未覆盖生产环境的完整治理、监控和扩展要求。

## 技术栈

- Python / FastAPI / SQLAlchemy / Pydantic / SQLite
- ChromaDB / Embedding / RAG / 来源引用 / 域外拒答
- OpenAI-compatible API / Function Calling / Agent 工具调用
- LangGraph StateGraph / Supervisor / 多 Agent 条件路由 / Repository Adapter
- pytest / JSONL evals / Docker / Swagger / SSE

## 快速开始

建议每个项目使用独立虚拟环境，避免依赖污染公司电脑上的全局 Python。以下命令适用于 Windows PowerShell；开始前先确认已安装 Python 3.11 或 3.12：

```powershell
python --version
```

### 1. RAG 金融文档问答

```powershell
cd rag-financial-qa

# 创建并激活项目独立虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖并运行验证
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest -q
python evals/run_eval.py --validate-only
python -m uvicorn app.main:app --reload --port 8000
```

打开 Swagger：`http://127.0.0.1:8000/docs`

无 API Key 时可以使用 mock mode 跑通接口链路。真实模型效果需要配置 `.env`：

```powershell
copy .env.example .env
```

### 2. 经营数据分析 Agent

打开一个新的 PowerShell 窗口，从仓库根目录执行：

```powershell
cd business-data-agent

# 每个项目单独创建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖并运行验证
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest -q
python evals/run_agent_eval.py
python scripts/smoke_demo.py
python -m uvicorn app.main:app --reload --port 8001
```

打开 Swagger：`http://127.0.0.1:8001/docs`

无 API Key 时默认使用 mock mode，可演示注册、数据源、自然语言分析、SQL 查询、工具轨迹和报告导出。

也可从仓库根目录复制 `.env.compose.example` 为 `.env`，替换本地密码和签名密钥后启动 MySQL、Redis 与 Agent：

```powershell
copy .env.compose.example .env
docker compose config --quiet
docker compose up -d mysql redis business-data-agent
```

该 Compose 仅是本地开发编排：Redis 当前作为基础设施预留，业务代码尚未接入缓存或任务队列；它也不包含金融 RAG 和电商服务，不能视为统一生产中台部署。

### 3. 跨境电商多 Agent 智能客服

```powershell
cd ecommerce-multi-agent-support
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
python evals/run_eval.py
python scripts/smoke_demo.py
python -m uvicorn app.main:app --reload --port 8002
```

打开 Swagger：`http://127.0.0.1:8002/docs`

该项目使用结构逼真的仿真数据验证 LangGraph 多 Agent 分工、Tool/Repository 边界、订单越权拦截和售后敏感动作控制。评测结果仅代表本地确定性 V1 路径。

如果公司电脑的 PowerShell 执行策略不允许运行 `Activate.ps1`，无需修改系统策略，可直接调用虚拟环境中的 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

演示结束后可执行 `deactivate` 退出虚拟环境。

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
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-langchain.txt
python examples/langchain_rag_demo.py --mock --question "2024年公司营业收入是多少？"

cd ..\business-data-agent
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-langchain.txt
python examples/langchain_sql_agent_demo.py --mock --question "2024年每月收入趋势如何？"
```

说明文档见：`docs/LANGCHAIN_COMPARISON.md`。

## 评测与测试

- RAG：`evals/questions.jsonl` 覆盖资料内事实、原因解释、风险问题、资料外问题和金融高风险拒答。
- Agent：`evals/agent_questions.jsonl` 覆盖工具选择、SQL 结构、结果行数和危险 SQL 拦截。
- 电商多 Agent：`evals/cases.jsonl` 覆盖四类路由、商品硬条件、订单越权、售后审批、拒答和提示注入。
- 三个项目均包含 pytest 测试，覆盖核心逻辑和安全边界。

## 本地验证记录

- `rag-financial-qa`：已在 mock mode 下验证，`pytest` 通过 16 个测试，`evals/run_eval.py --validate-only` 通过 24 条评测集结构校验；LangChain 对照 demo 可离线运行。
- `business-data-agent`：已在 mock mode 下验证，`pytest` 通过 24 个测试，`evals/run_agent_eval.py` 通过 7/7 条评测，LangChain SQL Agent 对照 demo 可离线运行。
- `ecommerce-multi-agent-support`：本地 V1.0 通过 55 项 pytest；30/30 条离线评测通过，路由与工具选择准确率均为 100%，4/4 安全案例通过。指标仅代表固定本地评测集和确定性 fallback 链路。

## 仓库安全说明

本仓库不提交 `.env`、API Key、本地运行数据库、上传文件、向量库数据、虚拟环境、缓存和日志。示例数据仅用于本地演示和评测。
