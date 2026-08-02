import json
import logging

import httpx
import sqlglot
from sqlglot import exp

from app.config import settings
from app.models.models import AnalysisRecord
from app.repositories import AnalysisRepository
from app.services.chart_service import create_chart
from app.utils.db_connector import DatabaseConnector
from app.utils.sql_safety import validate_sql, sanitize_sql

logger = logging.getLogger("kb_qa.agent")

# MIGRATION: legacy generic financial analyst prompt -> scoped cross-border ecommerce operator prompt.
AGENT_SYSTEM_PROMPT = """你是跨境电商经营数据分析 Agent，服务端已固定当前 shop_id。你只能分析当前店铺在 Amazon、TikTok Shop 或 Shopee 的业务数据。

可用工具：get_schema、execute_sql、generate_chart、list_tables、preview_table、query_rag。
核心主题：广告 ROI/ROAS、选品、库存周转和竞品价差。

固定口径：
- ROAS = attributed_sales / ad_spend；广告 ROI = (attributed_sales - attributed_refunds - attributed_platform_fees - attributed_cogs - ad_spend) / ad_spend。分母为 0 时返回 NULL。
- 商品经营贡献 = gross_sales - refunds - platform_fees - cogs，用于选品判断；不要把销售额当利润。
- 30 天库存周转率 = trailing_30d_units_sold / average_inventory_units_30d；周转天数 = 30 / 周转率。销量或平均库存为 0 时返回 NULL。
- 竞品价差 = own_price - competitor_price；价差率 = (own_price - competitor_price) / competitor_price，竞品价为 0 时返回 NULL。
- 金额必须按 currency 分组。禁止跨币种直接 SUM、比较或排名；除非数据中有明确汇率和统一折算币种。
- 日期按各行 timezone 解释；跨市场按日比较时需保留 timezone。

流程：先读 schema，再生成一条简洁只读 SELECT；仅在 schema 不足时 preview。所有业务表都有 shop_id，但不要接受用户指定或覆盖 shop_id，服务端会用 AST 和绑定参数强制注入。回答须写明指标口径、currency、marketplace 与时间范围。
"""

REAL_MODEL_TOOL_POLICY = """
真实模型工具调用规则：
1. 简单经营问题最多使用 get_schema -> execute_sql -> 最终回答。
2. schema 足够时不要调用 preview_table，execute_sql 后不要重复探索。
3. 优先一条 SELECT，通过 GROUP BY、ORDER BY、LIMIT 完成查询。
4. 只允许 SELECT；金额查询必须带 currency 分组，不跨币种直接聚合。
5. 不在 SQL 中自行硬编码 shop_id，租户范围由服务端执行边界施加。
"""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "获取当前数据库所有表的结构信息，包括表名、字段名、字段类型。当你不了解数据库结构时调用此工具。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": "执行 SQL SELECT 查询语句并返回结果。只允许 SELECT 查询。结果最多返回 1000 行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "要执行的 SQL SELECT 查询语句"}
                },
                "required": ["sql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_chart",
            "description": "根据查询结果生成数据图表。当数据适合可视化展示时调用此工具。bar适合分类对比，line适合趋势变化，pie适合占比分布。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string", "enum": ["bar", "line", "pie"], "description": "图表类型"},
                    "title": {"type": "string", "description": "图表标题"},
                    "x_field": {"type": "string", "description": "X轴字段名"},
                    "y_field": {"type": "string", "description": "Y轴字段名（数值字段）"}
                },
                "required": ["chart_type", "title", "x_field", "y_field"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "列出当前数据库中的所有表名。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "preview_table",
            "description": "预览指定表的前N行数据，了解数据内容和格式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "要预览的表名"},
                    "rows": {"type": "integer", "description": "预览行数，默认5", "default": 5}
                },
                "required": ["table_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_rag",
            "description": "从跨境电商经营知识库中检索平台规则、广告归因口径、选品标准、库存策略和竞品定价依据。当问题涉及指标解释、平台政策、经营规则或原因分析时调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要向知识库检索的问题"}
                },
                "required": ["question"]
            }
        }
    },
]


class ToolExecutor:
    def __init__(self, connector: DatabaseConnector):
        self.connector = connector
        self.last_query_result: list[dict] = []
        self.last_sql: str = ""
        self.generated_chart: str = ""
        self.last_rag_answer: str = ""
        self.rag_sources: list[dict] = []
        self.tool_trace: list[dict] = []

    async def execute(self, function_name: str, arguments: dict) -> str:
        logger.info(f"Tool called: {function_name}({arguments})")

        try:
            if function_name == "get_schema":
                result = self._get_schema()
            elif function_name == "execute_sql":
                result = self._execute_sql(arguments["sql"])
            elif function_name == "generate_chart":
                result = self._generate_chart(arguments)
            elif function_name == "list_tables":
                result = self._list_tables()
            elif function_name == "preview_table":
                result = self._preview_table(arguments)
            elif function_name == "query_rag":
                result = await self._query_rag(arguments)
            else:
                result = f"未知工具: {function_name}"
            self._append_trace(function_name, arguments, result)
            return result
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            result = f"工具执行错误: {str(e)}"
            self._append_trace(function_name, arguments, result, success=False)
            return result

    def _append_trace(self, function_name: str, arguments: dict, result: str, success: bool = True):
        self.tool_trace.append({
            "step": len(self.tool_trace) + 1,
            "tool": function_name,
            "arguments": arguments,
            "success": success and not str(result).startswith(("SQL验证失败", "SQL执行错误", "工具执行错误")),
            "result_preview": _compact_text(result),
        })

    def _get_schema(self) -> str:
        schema = self.connector.get_schema()
        if not schema:
            return "无法获取数据库结构，请检查数据库连接。"

        lines = ["数据库结构：\n"]
        for table in schema:
            lines.append(f"表 {table['table']}：")
            for col in table['columns']:
                pk = " (主键)" if col.get("primary_key") else ""
                lines.append(f"  {col['name']} {col['type']}{pk}")
            if table.get("foreign_keys"):
                for fk in table["foreign_keys"]:
                    lines.append(f"  外键: {fk['from']} -> {fk['to']}")
            lines.append("")
        return "\n".join(lines)

    def _execute_sql(self, sql: str) -> str:
        dialect = self.connector.engine.dialect.name
        is_valid, msg = validate_sql(sql, dialect=dialect)
        if not is_valid:
            return f"SQL验证失败: {msg}。请修改SQL，只允许SELECT查询。"

        sql = sanitize_sql(
            sql,
            settings.MAX_QUERY_ROWS,
            dialect=dialect,
        )
        try:
            _enforce_business_aggregation_policy(sql, dialect=dialect)
        except PermissionError as exc:
            return f"SQL业务口径验证失败: {exc}。金额聚合和排名必须保留 marketplace、currency 边界。"
        self.last_sql = sql

        try:
            result = self.connector.execute_query(sql, max_rows=settings.MAX_QUERY_ROWS)
            self.last_query_result = result

            if not result:
                return "查询结果为空，请检查SQL语句或尝试其他查询条件。"

            summary = f"查询返回 {len(result)} 行数据。\n"
            summary += f"前5行：\n{json.dumps(result[:5], ensure_ascii=False, default=str)}"
            return summary
        except Exception as e:
            return f"SQL执行错误: {str(e)}。请检查SQL语法并重试。"

    def _generate_chart(self, args: dict) -> str:
        if not self.last_query_result:
            return "没有可用的查询结果，请先执行 SQL 查询。"

        chart_type = args["chart_type"]
        title = args["title"]
        x_field = args["x_field"]
        y_field = args["y_field"]

        try:
            filename = create_chart(
                data=self.last_query_result,
                chart_type=chart_type,
                title=title,
                x_field=x_field,
                y_field=y_field,
            )
            self.generated_chart = filename
            return f"图表已生成: {filename}"
        except Exception as e:
            return f"图表生成失败: {str(e)}"

    def _list_tables(self) -> str:
        tables = self.connector.get_tables()
        if not tables:
            return "数据库中没有表。"
        return f"数据库中的表：{', '.join(tables)}"

    def _preview_table(self, args: dict) -> str:
        table_name = args["table_name"]
        rows = _bounded_preview_rows(args.get("rows"))
        try:
            result = self.connector.preview_table(table_name, rows)
            if not result:
                return f"表 {table_name} 为空。"
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            return f"预览失败: {str(e)}"

    async def _query_rag(self, args: dict) -> str:
        if not settings.RAG_ENABLED:
            return "知识库检索未启用。若需要结合平台规则、广告归因口径、选品标准、库存策略或竞品定价依据，请配置 RAG_ENABLED=true。"

        question = str(args.get("question", "")).strip()
        if not question:
            return "知识库检索失败：缺少要检索的问题。"

        if not settings.RAG_API_BASE_URL or not settings.RAG_ACCESS_TOKEN or not settings.RAG_CONVERSATION_ID:
            return "知识库检索未配置完整，请检查 RAG_API_BASE_URL、RAG_ACCESS_TOKEN 和 RAG_CONVERSATION_ID。"

        base_url = settings.RAG_API_BASE_URL.rstrip("/")
        url = f"{base_url}/api/chat/{settings.RAG_CONVERSATION_ID}"

        try:
            async with httpx.AsyncClient(timeout=settings.RAG_TIMEOUT) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {settings.RAG_ACCESS_TOKEN}"},
                    json={"question": question},
                )

            if response.status_code == 401:
                return "知识库检索认证失败，请检查 RAG_ACCESS_TOKEN 是否有效。"
            if response.status_code == 404:
                return "知识库对话不存在或无权访问，请检查 RAG_CONVERSATION_ID。"
            if response.status_code >= 400:
                return f"知识库检索失败：HTTP {response.status_code} - {response.text[:200]}"

            data = response.json()
        except httpx.ConnectError:
            return "知识库服务不可用，请确认第18章 RAG 服务已启动。"
        except httpx.TimeoutException:
            return "知识库检索超时，请稍后重试或调大 RAG_TIMEOUT。"
        except Exception as e:
            logger.error(f"RAG query failed: {e}")
            return f"知识库检索失败: {str(e)}"

        answer = data.get("answer", "")
        sources = data.get("sources") or []
        self.last_rag_answer = answer
        self.rag_sources = sources

        if not answer:
            return "知识库没有返回有效回答。"

        lines = [f"知识库回答：{answer}"]
        if sources:
            lines.append("来源：")
            for source in sources[:5]:
                document = source.get("document", "未知文档")
                relevance = source.get("relevance")
                if relevance is None:
                    lines.append(f"- {document}")
                else:
                    lines.append(f"- {document}，相关度 {relevance}")
        else:
            lines.append("来源：知识库未返回来源信息")

        return "\n".join(lines)


async def run_agent(question: str, connector: DatabaseConnector) -> dict:
    executor = ToolExecutor(connector)
    messages = [
        {"role": "system", "content": f"{AGENT_SYSTEM_PROMPT}\n\n{REAL_MODEL_TOOL_POLICY}"},
        {"role": "user", "content": question},
    ]

    if not settings.API_KEY:
        return await _run_mock_agent(question, executor)

    for step in range(settings.MAX_AGENT_STEPS):
        logger.info(f"Agent step {step + 1}/{settings.MAX_AGENT_STEPS}")

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{settings.BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.API_KEY}"},
                    json={
                        "model": settings.MODEL,
                        "messages": messages,
                        "tools": _tools_for_real_model(),
                        "tool_choice": "auto",
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return _agent_result(
                executor,
                f"大模型调用失败: {str(e)}",
                data_limit=20,
            )

        choice = data["choices"][0]
        finish_reason = choice["finish_reason"]
        assistant_message = choice["message"]

        if finish_reason == "stop":
            return _agent_result(executor, assistant_message.get("content", ""))

        if finish_reason == "tool_calls":
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls", [])
            for tool_call in tool_calls:
                function_name = tool_call["function"]["name"]
                try:
                    arguments = json.loads(tool_call["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                result = await executor.execute(function_name, arguments)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": _compact_tool_result(function_name, str(result)),
                })

    if executor.last_query_result:
        return _agent_result(executor, _build_fallback_answer(question, executor))

    return _agent_result(executor, "分析步骤过多，请简化您的问题或分步提问。")


async def _run_mock_agent(question: str, executor: ToolExecutor) -> dict:
    tables = executor.connector.get_tables()
    await executor.execute("get_schema", {})

    if "ad_performance" in tables and any(keyword in question.upper() for keyword in ["ROAS", "ROI", "广告"]):
        sql = """
            SELECT
                report_date,
                platform,
                marketplace,
                timezone,
                currency,
                ROUND(SUM(attributed_sales) / NULLIF(SUM(ad_spend), 0), 4) AS roas,
                ROUND((SUM(attributed_sales) - SUM(attributed_refunds) -
                       SUM(attributed_platform_fees) - SUM(attributed_cogs) - SUM(ad_spend)) /
                      NULLIF(SUM(ad_spend), 0), 4) AS ad_roi
            FROM ad_performance
            GROUP BY report_date, platform, marketplace, timezone, currency
            ORDER BY report_date, platform
        """
        await executor.execute("execute_sql", {"sql": sql})
        answer = (
            f"[模拟回答] 关于「{question}」，已按日期、平台、市场和币种计算广告效率。"
            "ROAS=归因销售额/广告花费；广告ROI=(归因销售额-归因退款-归因平台费-归因COGS-广告花费)/广告花费。"
            "结果保留 currency，不跨币种直接聚合。"
        )
    elif "inventory_snapshots" in tables and any(keyword in question for keyword in ["库存", "周转", "断货"]):
        sql = """
            SELECT
                snapshot_date,
                platform,
                marketplace,
                timezone,
                currency,
                sku,
                product_name,
                on_hand_units,
                CASE
                    WHEN on_hand_units IS NULL THEN NULL
                    WHEN on_hand_units = 0 THEN 1
                    ELSE 0
                END AS is_stockout,
                CASE WHEN on_hand_units IS NULL THEN 1 ELSE 0 END AS is_inventory_unknown,
                average_inventory_units_30d,
                trailing_30d_units_sold,
                CASE
                    WHEN trailing_30d_units_sold = 0 OR average_inventory_units_30d = 0 THEN NULL
                    ELSE ROUND(trailing_30d_units_sold / average_inventory_units_30d, 4)
                END AS inventory_turnover_rate_30d,
                CASE
                    WHEN trailing_30d_units_sold = 0 OR average_inventory_units_30d = 0 THEN NULL
                    ELSE ROUND(30.0 * average_inventory_units_30d / trailing_30d_units_sold, 2)
                END AS inventory_turnover_days
            FROM inventory_snapshots
            ORDER BY is_inventory_unknown DESC, is_stockout DESC, inventory_turnover_days
        """
        await executor.execute("execute_sql", {"sql": sql})
        answer = (
            f"[模拟回答] 关于「{question}」，30天库存周转率=近30天销量/30天平均库存；"
            "周转天数=30/周转率。销量或平均库存为0时返回NULL；on_hand_units=0 标记断货，"
            "on_hand_units 为 NULL 时独立标记 unknown inventory risk。"
        )
    elif "competitor_prices" in tables and any(keyword in question for keyword in ["竞品", "价差", "价格"]):
        sql = """
            SELECT
                observed_at,
                platform,
                marketplace,
                timezone,
                currency,
                sku,
                product_name,
                competitor_name,
                own_price,
                competitor_price,
                ROUND(own_price - competitor_price, 2) AS price_gap,
                ROUND((own_price - competitor_price) / NULLIF(competitor_price, 0), 4) AS price_gap_rate
            FROM competitor_prices
            ORDER BY price_gap_rate DESC
        """
        await executor.execute("execute_sql", {"sql": sql})
        answer = (
            f"[模拟回答] 关于「{question}」，竞品价差=自有价-竞品价；"
            "价差率=价差/竞品价。结果保留 marketplace、currency 和 observed_at。"
        )
    elif "sales_records" in tables and any(keyword in question for keyword in ["选品", "商品", "贡献", "利润"]):
        sql = """
            WITH product_contribution AS (
                SELECT
                    platform,
                    marketplace,
                    timezone,
                    currency,
                    MIN(order_date) AS period_start,
                    MAX(order_date) AS period_end,
                    sku,
                    product_name,
                    SUM(units_sold) AS units_sold,
                    ROUND(SUM(gross_sales - refunds - platform_fees - cogs), 2) AS operating_contribution
                FROM sales_records
                GROUP BY platform, marketplace, timezone, currency, sku, product_name
            )
            SELECT
                *,
                RANK() OVER (
                    PARTITION BY marketplace, currency
                    ORDER BY operating_contribution DESC
                ) AS contribution_rank
            FROM product_contribution
            ORDER BY marketplace, currency, contribution_rank, sku
        """
        await executor.execute("execute_sql", {"sql": sql})
        answer = (
            f"[模拟回答] 关于「{question}」，商品经营贡献=销售额-退款-平台费-COGS。"
            "结果按 marketplace、currency 分区排名，不把销售额当利润，也不跨币种排名；"
            "范围使用查询结果中的实际 period_start、period_end、marketplace、currency 与 timezone。"
        )
    else:
        answer = (
            f"[模拟回答] 关于「{question}」，当前库包含 {len(tables)} 个跨境电商业务表："
            f"{', '.join(tables)}。请询问广告ROAS/ROI、选品、库存周转或竞品价差。"
        )

    return _agent_result(executor, answer)


def _agent_result(
    executor: ToolExecutor,
    answer: str,
    data_limit: int = 50,
) -> dict:
    rows = executor.last_query_result[:data_limit]
    return {
        "answer": _append_result_scope(answer, rows),
        "sql_query": executor.last_sql,
        "data": rows,
        "chart_path": executor.generated_chart or None,
        "tool_trace": executor.tool_trace,
        "rag_sources": executor.rag_sources,
    }


def save_analysis_record(
    analyses: AnalysisRepository, question: str, answer: str, sql_query: str,
    data: list[dict], chart_path: str, ds_id: int, user_id: int, shop_id: str,
    tool_trace: list[dict] = None, rag_sources: list[dict] = None,
) -> AnalysisRecord:
    record = AnalysisRecord(
        question=question,
        answer=answer,
        sql_query=sql_query,
        query_result=json.dumps(data[:100], ensure_ascii=False, default=str),
        chart_path=chart_path or "",
        tool_trace=json.dumps(tool_trace or [], ensure_ascii=False, default=str),
        rag_sources=json.dumps(rag_sources or [], ensure_ascii=False, default=str),
        ds_id=ds_id,
        user_id=user_id,
        shop_id=shop_id,
    )
    return analyses.add(record)


def get_analysis_records(
    analyses: AnalysisRepository,
    user_id: int,
    shop_id: str,
    ds_id: int = None,
) -> list[AnalysisRecord]:
    return analyses.list_owned(user_id, shop_id, ds_id)


def _compact_text(value: str, max_chars: int = 1000) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def _enforce_business_aggregation_policy(sql: str, dialect: str | None = None) -> None:
    """Fail closed when monetary aggregation/ranking drops market-currency boundaries."""
    statement = sqlglot.parse_one(sql, read=dialect)
    monetary_columns = {
        "gross_sales", "refunds", "platform_fees", "cogs", "ad_spend",
        "attributed_sales", "attributed_refunds", "attributed_platform_fees",
        "attributed_cogs", "unit_cost", "own_price", "competitor_price",
        "operating_contribution", "price_gap", "price_gap_rate",
    }

    for select in statement.find_all(exp.Select):
        direct_tables = {
            table.name.casefold()
            for table in select.find_all(exp.Table)
            if table.find_ancestor(exp.Select) is select
        }
        reads_cross_border_facts = bool(direct_tables & {
            "sales_records", "ad_performance", "inventory_snapshots", "competitor_prices",
        })
        has_monetary_aggregate = any(
            isinstance(function, (exp.Sum, exp.Avg))
            and any(
                column.name.casefold() in monetary_columns
                for column in function.find_all(exp.Column)
            )
            for function in select.walk()
        )
        windows = list(select.find_all(exp.Window))
        has_monetary_rank = any(
            isinstance(window.this, (exp.Rank, exp.DenseRank, exp.RowNumber))
            and any(
                column.name.casefold() in monetary_columns
                for ordered in window.find_all(exp.Ordered)
                for column in ordered.find_all(exp.Column)
            )
            for window in windows
        )
        if not ((reads_cross_border_facts and has_monetary_aggregate) or has_monetary_rank):
            continue

        group = select.args.get("group")
        group_columns = {
            column.name.casefold()
            for column in (group.expressions if group is not None else [])
            for column in column.find_all(exp.Column)
        }
        if reads_cross_border_facts and has_monetary_aggregate and not {
            "marketplace", "currency",
        } <= group_columns:
            raise PermissionError("金额聚合必须按 marketplace、currency 分组")

        for window in windows:
            if not isinstance(window.this, (exp.Rank, exp.DenseRank, exp.RowNumber)):
                continue
            if not any(
                column.name.casefold() in monetary_columns
                for ordered in window.find_all(exp.Ordered)
                for column in ordered.find_all(exp.Column)
            ):
                continue
            partition_columns = {
                column.name.casefold()
                for expression in (window.args.get("partition_by") or [])
                for column in expression.find_all(exp.Column)
            }
            if not {"marketplace", "currency"} <= partition_columns:
                raise PermissionError("金额排名必须按 marketplace、currency partition")


def _tools_for_real_model() -> list[dict]:
    tools = json.loads(json.dumps(TOOLS, ensure_ascii=False))
    for tool in tools:
        function = tool.get("function", {})
        if function.get("name") == "preview_table":
            function["description"] = (
                "仅预览少量样例行。当 schema 已经足够判断表和字段时，不要在简单趋势、排名、"
                "分组或聚合问题中调用本工具，应直接调用 execute_sql。"
            )
            rows_schema = function.get("parameters", {}).get("properties", {}).get("rows", {})
            rows_schema["default"] = settings.MAX_PREVIEW_ROWS
            rows_schema["maximum"] = settings.MAX_PREVIEW_ROWS
    return tools


def _bounded_preview_rows(value) -> int:
    try:
        rows = int(value if value is not None else settings.MAX_PREVIEW_ROWS)
    except (TypeError, ValueError):
        rows = settings.MAX_PREVIEW_ROWS
    return max(1, min(rows, settings.MAX_PREVIEW_ROWS))


def _compact_tool_result(function_name: str, result: str) -> str:
    max_chars = settings.MAX_TOOL_RESULT_CHARS
    if function_name == "preview_table":
        max_chars = min(max_chars, 1200)
    if function_name == "get_schema":
        max_chars = min(max_chars, 1800)
    return _compact_text(result, max_chars)


def _build_fallback_answer(question: str, executor: ToolExecutor) -> str:
    rows = executor.last_query_result
    if not rows:
        return "分析步骤过多，请简化您的问题或分步提问。"

    first_row = rows[0]
    columns = list(first_row.keys())
    lines = [
        f"已完成数据查询，但模型在 {settings.MAX_AGENT_STEPS} 轮工具调用内未生成最终总结。",
        f"问题：{question}",
        f"本次 SQL 返回 {len(rows)} 行，主要字段包括：{', '.join(columns)}。",
    ]

    if "report_date" in first_row and len(rows) >= 2:
        requested = question.casefold()
        metrics = []
        if "roi" in requested and "ad_roi" in first_row:
            metrics.append("ad_roi")
        if "roas" in requested and "roas" in first_row:
            metrics.append("roas")
        if not metrics:
            metrics = [metric for metric in ("roas", "ad_roi") if metric in first_row]
        for metric in metrics:
            start = rows[0].get(metric)
            end = rows[-1].get(metric)
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                direction = "上升" if end > start else "下降" if end < start else "基本持平"
                lines.append(f"按报告日期看，{metric} 从 {start} 变化到 {end}，整体呈{direction}趋势。")

    if "inventory_turnover_days" in first_row:
        valid = [
            row for row in rows
            if isinstance(row.get("inventory_turnover_days"), (int, float))
        ]
        if valid:
            fastest = min(valid, key=lambda row: row["inventory_turnover_days"])
            lines.append(
                f"库存周转最快的 SKU 是 {fastest.get('sku')}，约 "
                f"{fastest['inventory_turnover_days']} 天。"
            )
        else:
            lines.append("当前结果缺少可计算的库存周转天数，请检查平均库存和近30天销量。")
        stockouts = [row.get("sku") for row in rows if row.get("on_hand_units") == 0]
        if stockouts:
            lines.append(f"断货风险 SKU：{', '.join(str(sku) for sku in stockouts if sku)}。")
        unknown_inventory = [
            row.get("sku") for row in rows if row.get("on_hand_units") is None
        ]
        if unknown_inventory:
            lines.append(
                "Unknown inventory risk SKU："
                f"{', '.join(str(sku) for sku in unknown_inventory if sku)}。"
            )

    scope = _result_scope(rows)
    if scope:
        lines.append(f"查询结果实际范围：{scope}。")
    lines.append("请结合 sql_query 与 data 复核指标口径和租户边界。")
    return "\n".join(lines)


def _append_result_scope(answer: str, rows: list[dict]) -> str:
    scope = _result_scope(rows)
    if not scope:
        return answer
    scope_line = f"查询结果实际范围：{scope}。"
    normalized_answer = "".join(str(answer).split()).casefold()
    normalized_scope = "".join(scope_line.split()).casefold()
    if normalized_scope in normalized_answer:
        return answer
    return f"{answer.rstrip()}\n{scope_line}"


def _result_scope(rows: list[dict]) -> str:
    marketplaces = sorted({
        str(row["marketplace"])
        for row in rows
        if row.get("marketplace") not in (None, "")
    })
    currencies = sorted({
        str(row["currency"])
        for row in rows
        if row.get("currency") not in (None, "")
    })
    timezones = sorted({
        str(row["timezone"])
        for row in rows
        if row.get("timezone") not in (None, "")
    })
    time_fields = (
        "report_date", "order_date", "snapshot_date", "observed_at",
        "period_start", "period_end",
    )
    time_values = [
        str(row[field])
        for row in rows
        for field in time_fields
        if row.get(field) not in (None, "")
    ]

    parts = []
    if time_values:
        parts.append(f"时间 {min(time_values)} 至 {max(time_values)}")
    if marketplaces:
        parts.append(f"marketplace {', '.join(marketplaces)}")
    if currencies:
        parts.append(f"currency {', '.join(currencies)}")
    if timezones:
        parts.append(f"timezone {', '.join(timezones)}")
    return "；".join(parts)
