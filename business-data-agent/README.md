# 企业经营数据智能分析 Agent

基于 Agent + Function Calling 的财务经营数据分析原型，支持自然语言查询数据库、自动生成只读 SQL、执行查询、生成图表，并导出 CSV 和 Markdown 分析报告。

> 当前项目用于验证经营数据分析 Agent 的核心后端链路，包括工具调用、SQL 安全、结果保存、图表生成和报告导出。项目尚未按生产级 BI 平台要求建设。

## 一、项目亮点

- **Function Calling Agent**：模型返回结构化 tool_calls，后端执行工具并回传结果。
- **财务经营场景化**：围绕收入、成本、毛利、预算、应收账款、现金流设计样例数据。
- **自然语言查数**：用户用中文提问，Agent 自动获取 schema、生成 SQL、执行查询。
- **SQL 安全控制**：使用 sqlglot AST 限制为单条只读 SELECT，二次拦截危险关键字，并自动添加顶层 LIMIT。
- **过程可追溯**：保存 Agent 工具调用轨迹，支持复核每一步工具、参数和结果摘要。
- **Agent 评测集**：固定问题集评估工具选择、SQL 关键结构、结果行数和安全拦截。
- **可视化与报告**：支持 bar/line/pie 图表，支持 CSV 和 Markdown 报告导出。
- **后端工程完整性**：FastAPI 分层架构、JWT 鉴权、数据源管理、分析记录保存、pytest 测试。

## 二、快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，配置 MySQL 和模型 API：

```env
DATABASE_URL=mysql+pymysql://financial_app:financial_app_password@localhost:3306/financial_platform?charset=utf8mb4
API_KEY=your-api-key
BASE_URL=https://api.openai.com/v1
MODEL=gpt-4o-mini
```

> 推荐在仓库根目录运行 `docker compose up -d mysql redis` 启动本地基础设施。不配置 API_KEY 也可以运行，系统会使用模拟模式。

如需把旧版 SQLite 元数据迁移到 MySQL，在仓库根目录执行：

```bash
python business-data-agent/migrate_sqlite_to_mysql.py --target "mysql+pymysql://financial_app:financial_app_password@localhost:3306/financial_platform?charset=utf8mb4"
```

脚本默认读取 `business-data-agent/storage/data_analyst.db`，并迁移 `users`、`datasources`、`analysis_records`；旧版数据库位于其他目录时必须通过 `--source` 显式指定。目标表非空时脚本默认拒绝执行，只有确认覆盖现有元数据后才可添加 `--replace-existing`；`tool_trace` 作为分析记录字段随记录迁移。

### 3. 运行自动化测试

```bash
pytest
```

测试会使用隔离的临时 SQLite 数据库，不会污染本地演示数据。

### 4. 端到端烟雾演示

不启动服务也可以快速验证注册、数据源、自然语言分析和报告导出：

```bash
python scripts/smoke_demo.py
```

如果要使用真实模型配置：

```bash
python scripts/smoke_demo.py --real-llm
```

### 5. 运行 Agent 评测集

```bash
python evals/run_agent_eval.py
```

输出 JSON：

```bash
python evals/run_agent_eval.py --json
```

### 6. 启动服务

```bash
cd demo
uvicorn app.main:app --reload --port 8000
```

### 7. 访问 Swagger

打开：

```text
http://localhost:8000/docs
```

## 三、最小演示流程

```text
1. 注册/登录，获取 Token
2. 使用系统内置 SQLite 财务经营样例库
3. 查看表结构和数据预览
4. 用自然语言提问
5. Agent 调用 get_schema / execute_sql / generate_chart 等工具
6. 查看回答、SQL、结果数据、图表和工具调用轨迹
7. 导出 CSV 或 Markdown 分析报告
```

推荐演示问题：

- 2024 年每月收入趋势如何？
- 各产品线毛利率是多少？
- 收入贡献最高的前 5 个客户是谁？
- 哪些部门预算执行率最高？
- 逾期应收账款金额最高的客户是谁？
- 哪些月份净现金流为负？

## 四、示例数据规划

当前项目使用内置 SQLite 财务经营样例库，包含：

| 表名 | 说明 |
|---|---|
| `revenue_records` | 收入、成本、毛利、客户、产品线、区域、回款状态 |
| `expense_records` | 部门费用、费用类型、金额、供应商 |
| `budget_records` | 部门预算、实际金额、预算执行情况 |
| `receivables` | 应收账款、到期日、已回款、逾期天数、状态 |
| `cashflow_records` | 现金流入、流出、净现金流、类别 |

## 五、API 接口说明

### 认证

- `POST /api/auth/register` - 注册
- `POST /api/auth/login` - 登录
- `GET /api/auth/me` - 获取当前用户

### 数据源

- `GET /api/datasources` - 数据源列表
- `POST /api/datasources` - 添加数据源
- `GET /api/datasources/{id}/schema` - 获取表结构
- `GET /api/datasources/{id}/tables` - 获取表列表
- `GET /api/datasources/{id}/preview/{table}` - 预览表数据
- `DELETE /api/datasources/{id}` - 删除数据源

### 智能分析

- `POST /api/analysis/ask` - 自然语言提问
- `GET /api/analysis/records` - 分析历史
- `GET /api/analysis/records/{id}` - 分析详情
- `GET /api/analysis/export/csv/{id}` - 导出 CSV
- `GET /api/analysis/export/report/{id}` - 导出分析报告

## 六、项目结构

```text
app/
├── main.py                 # 应用入口，中间件，示例数据初始化
├── config.py               # 配置管理
├── database.py             # 元数据库连接
├── models/models.py        # SQLAlchemy ORM 模型
├── schemas/schemas.py      # Pydantic 请求/响应模型
├── routers/
│   ├── auth.py             # 认证
│   ├── datasources.py      # 数据源管理
│   └── analysis.py         # 智能分析 + 导出
├── services/
│   ├── auth_service.py
│   ├── datasource_service.py
│   ├── agent_service.py    # Agent 核心（Function Calling 循环）
│   └── chart_service.py    # 图表生成
└── utils/
    ├── sql_safety.py       # SQL 安全检查
    └── db_connector.py     # 数据库连接器
```

## 七、核心技术点

| 技术点 | 实现方式 |
|---|---|
| Agent 循环 | Function Calling 多轮工具调用，最大步数限制 |
| 工具定义 | `get_schema`, `execute_sql`, `generate_chart`, `list_tables`, `preview_table` |
| SQL 安全 | sqlglot AST 单语句只读校验 + 危险关键字二次过滤 + 顶层 LIMIT |
| 过程追踪 | 保存 `tool_trace` 和 `rag_sources`，分析详情和 Markdown 报告可查看 |
| 图表生成 | Matplotlib 动态生成 bar/line/pie |
| 数据源管理 | SQLAlchemy 连接数据库，读取 schema 和表数据 |
| 错误恢复 | SQL 错误返回给 Agent，尝试修正 |
| 数据导出 | CSV 导出 + Markdown 分析报告 |
| 自动化测试 | pytest 覆盖 SQL 安全、鉴权、权限隔离、分析链路和报告导出 |
| Agent 评测 | `evals/agent_questions.jsonl` 覆盖工具选择、SQL 结构、行数和安全拦截 |

## 八、项目能力体现

1. **Agent 开发能力**
   - 理解 Function Calling 的工具调用流程。
   - 能设计工具 schema。
   - 能实现模型请求工具、后端执行工具、结果回传模型的循环。

2. **数据分析能力**
   - 能把自然语言问题转成 SQL 查询。
   - 能围绕收入、毛利、预算、应收和现金流做分析。
   - 能生成图表和报告。

3. **安全意识**
   - 不直接执行任意 SQL。
   - 执行前做 SELECT-only 和危险关键字校验。
   - 用最大步数限制 Agent 循环。
   - 保存工具调用轨迹，便于审计和排错。

4. **后端工程能力**
   - FastAPI API 设计。
   - SQLAlchemy 数据库连接。
   - JWT 鉴权。
   - 分析记录持久化和导出接口。
   - pytest 自动化测试和 Dockerfile 复现环境。

## 九、设计取舍与边界

### 1. Agent 和普通问答的区别

普通问答只生成文本，不能直接查询数据库。这个项目让模型通过 Function Calling 调用后端工具，例如获取 schema、执行只读 SQL、生成图表，并基于真实查询结果回答。

### 2. Function Calling 的作用

模型以结构化格式返回工具调用请求，包括工具名和参数。后端拿到 tool calls 后执行对应工具，再把工具结果回传给模型，最后生成分析结论。

### 3. SQL 安全控制

执行前统一经过 `sqlglot` AST 安全校验：只允许单条只读 SELECT（含 CTE、JOIN、聚合和子查询），遍历 AST 拦截 DML、DDL、管理命令和嵌套危险节点；AST 通过后再做危险关键字二次校验，并为顶层无 LIMIT 的查询添加行数限制。生产环境还需要只读账号、权限隔离、查询超时和审计日志。

### 4. 工具轨迹

保存 SQL 和 `tool_trace`，可以复核结论来自哪条查询、调用了哪些工具、每一步工具返回了什么结果。这个设计也便于定位模型工具选择错误或 SQL 执行错误。

### 5. mock mode

未配置 API Key 时，系统会进入 mock mode，仍然可以跑通注册、数据源、SQL 查询、工具轨迹保存和报告导出。配置 API Key 后可验证真实 Function Calling 分支。

### 6. 当前边界

- 元数据（用户、数据源配置、分析记录和工具轨迹）默认持久化到 MySQL 8.0，并由 Repository + FastAPI 依赖注入统一访问；测试继续使用隔离 SQLite。
- 内置财务经营样例库仍使用 SQLite，它是 Agent 查询的数据源，不属于平台元数据库迁移范围。
- SQL 安全已升级为应用层 AST Guardrail；生产环境仍需要数据库只读账号、表字段白名单、查询超时、资源配额和审计日志。
- 当前没有完整前端，主要通过 Swagger、脚本和导出报告完成验证。

## 十、扩展文档与运行注意事项

- 项目技术说明：`docs/PROJECT_OVERVIEW.md`
- 演示与验证流程：`docs/DEMO_RUNBOOK.md`
- 首次运行会自动创建示例数据库。
- 生成的图表保存在 `charts/` 目录。
- 不配置 API_KEY 时使用 mock mode，可验证后端链路。
- 生产环境请修改 SECRET_KEY、关闭 DEBUG，并限制 CORS。

## 十一、LangChain 对照 demo

主项目保留手写 Function Calling Agent，`examples/` 中额外提供 LangChain tools / agent 对照实现：

```bash
pip install -r requirements.txt
pip install -r requirements-langchain.txt
python examples/langchain_sql_agent_demo.py --mock --question "2024年每月收入趋势如何？"
```

该 demo 复用 `DatabaseConnector`、`validate_sql` 和 `sanitize_sql`，用 LangChain `@tool` 封装 `list_tables`、`get_schema`、`execute_sql`，无 API Key 时离线展示工具链，有 API Key 时可用 `create_agent` 让模型选择工具。
