# 企业经营数据智能分析 Agent

基于 Agent + Function Calling 的财务经营数据分析原型，支持自然语言查询数据库、自动生成只读 SQL、执行查询、生成图表，并导出 CSV 和 Markdown 分析报告。

> 当前项目定位：求职展示型原型。目标是完整展示 Agent 工具调用、SQL 安全和经营数据分析链路，不夸大为生产级 BI 平台。

## 一、项目亮点

- **Function Calling Agent**：模型返回结构化 tool_calls，后端执行工具并回传结果。
- **财务经营场景化**：围绕收入、成本、毛利、预算、应收账款、现金流设计样例数据。
- **自然语言查数**：用户用中文提问，Agent 自动获取 schema、生成 SQL、执行查询。
- **SQL 安全控制**：只允许 SELECT，拦截危险关键字、多语句和注释，自动添加 LIMIT。
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

复制 `.env.example` 为 `.env`，填入你的 API 配置：

```env
API_KEY=your-api-key
BASE_URL=https://api.openai.com/v1
MODEL=gpt-3.5-turbo
```

> 不配置 API_KEY 也可以运行，系统会使用模拟模式。

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
| SQL 安全 | 只允许 SELECT + 危险关键字过滤 + 注释过滤 + 多语句过滤 + 自动 LIMIT |
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

## 九、面试官可能会问

### Q1：这个 Agent 和普通大模型问答有什么区别？

普通问答只是模型直接生成文本，不能真正查询数据库。这个 Agent 可以根据问题调用工具，比如获取 schema、执行 SQL、生成图表，并基于真实查询结果回答。

### Q2：Function Calling 在项目里做什么？

Function Calling 让模型以结构化格式返回工具调用请求，包括工具名和参数。后端拿到 tool_calls 后执行对应工具，再把工具结果回传给模型，模型最后生成分析结论。

### Q3：为什么 Agent 要先获取 schema？

因为模型不知道数据库真实有哪些表和字段。先获取 schema，可以减少编造表名和字段名的问题，让 SQL 更可靠。

### Q4：如何防止危险 SQL？

执行前做 SQL 安全校验，只允许 SELECT，拦截 INSERT、UPDATE、DELETE、DROP、ALTER 等危险关键字，禁止多语句和注释，并自动添加 LIMIT。生产环境还需要只读账号、权限隔离和审计。

### Q5：如果模型生成了错误 SQL 怎么办？

工具执行 SQL 时会返回错误信息，Agent 可以根据错误继续修正。为了避免无限循环，系统限制最大工具调用步数。

### Q6：为什么要保存 SQL 和工具调用轨迹？

数据分析 Agent 必须可解释。保存 SQL 能让用户知道结论来自哪个查询，保存工具轨迹能帮助开发者调试 Agent 的执行过程。

### Q7：图表如何生成？

Agent 执行查询后，如果数据适合可视化，就调用 generate_chart 工具。后端用 Matplotlib 生成 PNG 图表，并把路径保存到分析记录里。

### Q8：项目局限是什么？

目前是求职展示型原型，主要验证 Function Calling Agent 工具链。当前主要基于 SQLite 样例库，SQL 安全是基础规则，复杂多轮经营分析、生产权限控制和真实企业数据接入还需要继续完善。

### Q9：没有 API Key 时怎么演示？

系统会进入 mock 模式，仍然可以跑通注册、数据源、SQL 查询、工具轨迹保存和报告导出。这样面试现场不依赖外部模型服务；配置 API Key 后再展示真实 Function Calling 能力。

## 十、简历写法建议

**企业经营数据智能分析 Agent｜FastAPI / Function Calling / SQLAlchemy**

- 基于 FastAPI、SQLAlchemy、SQLite 和 OpenAI-compatible Function Calling 实现财务经营数据分析 Agent，支持通过自然语言查询业务数据库并生成分析结论。
- 设计 `get_schema`、`execute_sql`、`generate_chart`、`list_tables`、`preview_table` 等工具链，由 Agent 根据用户问题动态选择工具并完成多步分析。
- 围绕收入趋势、产品线毛利率、预算执行和应收账款风险构建财务经营样例库，支持 SQL 查询、图表生成、分析记录保存和报告导出。
- 实现 SQL 安全控制和过程追踪，仅允许 SELECT 查询，拦截危险关键字、多语句和注释，自动追加 LIMIT，并持久化 Agent 工具调用轨迹，降低工具调用风险并提升可解释性。
- 使用 pytest 和 Agent 评测集覆盖 SQL 安全、鉴权、数据源权限隔离、工具选择、SQL 结构、mock Agent 分析链路和报告导出，保证项目可复现演示。

## 十一、注意事项

- 首次运行会自动创建示例数据库。
- 生成的图表保存在 `charts/` 目录。
- 不配置 API_KEY 时使用模拟模式，适合学习代码逻辑。
- 生产环境请修改 SECRET_KEY、关闭 DEBUG，并限制 CORS。
- 当前演示版本主要基于 SQLite 样例库，不夸大为生产级多数据库平台。
- SQL 安全是 demo 级基础防护，真实生产环境还需要只读账号、权限隔离、超时控制和审计日志。
- 面向简历展示的说明可查看 `docs/RESUME_PROJECT.md`。
