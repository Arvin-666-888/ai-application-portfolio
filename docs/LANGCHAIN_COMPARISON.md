# LangChain 对照实现说明

本仓库包含两条实现路径：主项目保留手写 RAG / Agent 链路，`examples/` 下提供 LangChain 对照 demo。这样做的目的不是替换主项目，而是用同一批样例数据对比底层实现和框架抽象的差异。

## RAG 对照

运行：

```powershell
cd rag-financial-qa
pip install -r requirements.txt
pip install -r requirements-langchain.txt
python examples/langchain_rag_demo.py --mock --question "2024年公司营业收入是多少？"
python examples/langchain_rag_demo.py --mock --question "竞争对手A公司收入是多少？"
```

实现点：

- 使用 LangChain `Document` 承载文档内容和 `source` metadata。
- 使用 `RecursiveCharacterTextSplitter` 对照主项目的递归文本切分逻辑。
- 使用 Chroma retriever 完成 top-k 检索，并输出 `answer`、`sources`、`snippet` 和 `relevance`。
- 无 API Key 时使用本地 `HashEmbeddings` 跑通流程；有 API Key 时可切换到 `OpenAIEmbeddings` 和 `ChatOpenAI`。

## Agent 对照

运行：

```powershell
cd business-data-agent
pip install -r requirements.txt
pip install -r requirements-langchain.txt
python examples/langchain_sql_agent_demo.py --mock --question "2024年每月收入趋势如何？"
```

实现点：

- 复用主项目的 `DatabaseConnector`、`validate_sql`、`sanitize_sql`。
- 使用 LangChain `@tool` 封装 `list_tables`、`get_schema`、`execute_sql`。
- `execute_sql` 仍然复用主项目 SQL 安全逻辑，只允许 SELECT，并拦截危险 SQL。
- 无 API Key 时离线展示工具注册和固定工具链；有 API Key 时使用 `create_agent` 让模型选择工具。
- 输出 `tool_trace`，与主项目工具轨迹结构保持一致。

## 设计取舍

- 主项目手写核心链路，便于控制数据流、权限校验、错误处理和评测逻辑。
- LangChain demo 只放在 `examples/` 中，用于展示同类能力如何映射到框架抽象。
- 两条路径共用样例数据和安全逻辑，避免出现两套互相不一致的业务规则。
- 当前对照 demo 重点验证框架使用方式，不承担完整后端接口、鉴权和持久化职责。

## 边界说明

- 主项目保留独立实现路径，LangChain 只作为对照实现。
- 当前 Agent demo 不是生产级 Agent 平台，仍需要更完整的权限、审计、资源配额和监控。
- 当前 RAG demo 使用小规模样例文档，真实语义质量需要配置真实 Embedding 和 Chat 模型后再评估。
