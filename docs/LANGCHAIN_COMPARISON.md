# LangChain 对照实现说明

这个仓库的主线仍然是两个手写项目：`rag-financial-qa` 展示从文档解析、切分、Embedding、ChromaDB 检索到 Prompt 生成的完整 RAG 链路；`business-data-agent` 展示手写 Function Calling 循环、工具执行、SQL 安全和工具轨迹。

新增的 LangChain demo 不是替换主项目，而是第二版对照实现，用来说明我不仅能理解底层链路，也能使用主流框架把同类能力快速封装出来。

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

- 用 LangChain `Document` 承载文档内容和 `source` metadata。
- 用 `RecursiveCharacterTextSplitter` 对照主项目的手写递归切分器。
- 用 LangChain Chroma retriever 完成 top-k 检索。
- 无 API Key 时用本地 `HashEmbeddings` 跑通流程；有 API Key 时可切换到 `OpenAIEmbeddings` 和 `ChatOpenAI`。
- 输出 `answer / sources / snippet / relevance`，便于和主项目接口返回做对比。

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
- 用 LangChain `@tool` 封装 `list_tables`、`get_schema`、`execute_sql`。
- `execute_sql` 仍然只允许 SELECT，危险 SQL 会被主项目安全逻辑拦截。
- 无 API Key 时离线演示工具注册和固定工具链；有 API Key 时用 `create_agent` 让模型选择工具。
- 输出 `tool_trace`，和主项目的工具轨迹设计保持一致。

## 面试表达

可以这样说：

> 我的主项目没有直接依赖 LangChain，而是手写了一遍 RAG 和 Agent 的核心链路，这样我能讲清楚每一步为什么存在。之后我补了 LangChain 对照实现：RAG 侧用 Document、TextSplitter、Chroma retriever，Agent 侧用 @tool 和 create_agent，把同样的能力用框架抽象再实现一遍。这样既能展示底层理解，也能说明我会用主流框架提高开发效率。

不要这样夸大：

- 不写“主项目基于 LangChain 重构”。
- 不写“精通 LangChain / LangGraph”。
- 不写“生产级 Agent 平台”。

简历中稳妥写法：

- 补充 LangChain 对照 demo，使用 `Document`、`RecursiveCharacterTextSplitter`、Chroma retriever 验证同一批金融样例文档的检索流程，并与手写 RAG 链路进行对比。
- 补充 LangChain tools demo，将表结构查询和只读 SQL 执行封装为工具，对比手写 ToolExecutor 与 `create_agent` 编排方式。
