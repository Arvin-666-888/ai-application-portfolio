import json
import logging

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import AnalysisRecord
from app.services.chart_service import create_chart
from app.utils.db_connector import DatabaseConnector
from app.utils.sql_safety import validate_sql, sanitize_sql

logger = logging.getLogger("kb_qa.agent")

AGENT_SYSTEM_PROMPT = """你是一个专业的企业经营数据分析 Agent。你既可以查询内部结构化经营数据库，也可以检索企业知识库中的财报、公告、制度、指标口径和业务说明文档。

你可以使用以下工具：
1. get_schema - 获取数据库表结构
2. execute_sql - 执行 SQL 查询（只允许 SELECT）
3. generate_chart - 生成数据图表（bar柱状图/line折线图/pie饼图）
4. list_tables - 列出所有表
5. preview_table - 预览表数据
6. query_rag - 检索企业知识库，获取非结构化文档依据和来源

工具选择规则：
- 涉及金额、趋势、排名、分组统计、同比环比、预算执行、应收风险等结构化数据问题时，使用 SQL 工具链。
- 涉及制度、政策、指标口径、业务规则、财报公告、外部背景、原因解释等非结构化知识时，调用 query_rag。
- 如果用户问题同时要求“查数据”和“解释原因/判断是否符合规则”，先用 SQL 查询数据，再用 query_rag 检索文档依据，最后综合回答。

工作流程：
1. 判断问题属于结构化数据、非结构化知识，还是混合分析
2. 需要查数时，先了解数据库结构（调用 get_schema 或 list_tables）
3. 根据用户需求生成并执行只读 SQL 查询
4. 需要业务口径或文档依据时，调用 query_rag
5. 如果数据适合可视化，生成图表
6. 用中文总结分析结果，并区分“数据结论”和“文档依据”

注意事项：
- 只生成 SELECT 查询，不要生成修改数据的语句
- SQL 要高效，避免全表扫描
- 查询结果过多时使用 LIMIT 限制
- 不要编造知识库中没有的制度、政策或指标口径
- 当数据包含分类对比时，考虑使用柱状图
- 当数据包含时间趋势时，考虑使用折线图
- 当数据包含占比分布时，考虑使用饼图
"""

REAL_MODEL_TOOL_POLICY = """
真实模型工具调用稳定性规则：
1. 对于收入趋势、成本、毛利率、排名、分组、聚合等简单结构化数据问题，最多使用：get_schema -> execute_sql -> 最终回答。
2. 当 schema 已经能确认所需表和字段时，不要为简单趋势、排名、分组或聚合问题调用 preview_table。
3. 只有当用户明确要求查看样例数据，或仅凭 schema 无法判断分类取值时，才调用 preview_table。
4. execute_sql 返回数据后，直接基于 SQL 结果回答，不要再次调用 get_schema、list_tables 或 preview_table。
5. 优先生成一条简洁的 SELECT，通过 GROUP BY、ORDER BY、LIMIT 完成查询，避免多次探索性工具调用。
6. 禁止生成修改数据的 SQL，只允许 SELECT 查询。
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
            "description": "从企业知识库中检索财报、公告、制度、指标口径、业务说明等非结构化文档依据。当用户问题涉及指标解释、业务规则、政策制度、外部背景或原因分析时调用此工具。",
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
        is_valid, msg = validate_sql(sql)
        if not is_valid:
            return f"SQL验证失败: {msg}。请修改SQL，只允许SELECT查询。"

        sql = sanitize_sql(sql, settings.MAX_QUERY_ROWS)
        self.last_sql = sql

        try:
            result = self.connector.execute_query(sql)
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
            return "知识库检索未启用。若需要结合财报、公告、制度或指标口径，请配置 RAG_ENABLED=true。"

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
            return {
                "answer": f"大模型调用失败: {str(e)}",
                "sql_query": executor.last_sql,
                "data": executor.last_query_result[:20],
                "chart_path": executor.generated_chart or None,
                "tool_trace": executor.tool_trace,
                "rag_sources": executor.rag_sources,
            }

        choice = data["choices"][0]
        finish_reason = choice["finish_reason"]
        assistant_message = choice["message"]

        if finish_reason == "stop":
            answer = assistant_message.get("content", "")
            return {
                "answer": answer,
                "sql_query": executor.last_sql,
                "data": executor.last_query_result[:50],
                "chart_path": executor.generated_chart or None,
                "tool_trace": executor.tool_trace,
                "rag_sources": executor.rag_sources,
            }

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
        return {
            "answer": _build_fallback_answer(question, executor),
            "sql_query": executor.last_sql,
            "data": executor.last_query_result[:50],
            "chart_path": executor.generated_chart or None,
            "tool_trace": executor.tool_trace,
            "rag_sources": executor.rag_sources,
        }

    return {
        "answer": "分析步骤过多，请简化您的问题或分步提问。",
        "sql_query": executor.last_sql,
        "data": executor.last_query_result[:50],
        "chart_path": executor.generated_chart or None,
        "tool_trace": executor.tool_trace,
        "rag_sources": executor.rag_sources,
    }


async def _run_mock_agent(question: str, executor: ToolExecutor) -> dict:
    tables = executor.connector.get_tables()

    if "revenue_records" in tables and "毛利率" in question and any(keyword in question for keyword in ["产品线", "各产品"]):
        sql = """
            SELECT
                product_line,
                SUM(revenue) AS total_revenue,
                SUM(gross_profit) AS total_gross_profit,
                ROUND(SUM(gross_profit) * 1.0 / SUM(revenue), 4) AS gross_margin
            FROM revenue_records
            GROUP BY product_line
            ORDER BY gross_margin DESC
        """
        await executor.execute("get_schema", {})
        await executor.execute("execute_sql", {"sql": sql})
        answer = f"[模拟回答] 关于「{question}」，系统已按产品线汇总 revenue_records 表。\n"
        answer += "毛利率口径为 SUM(gross_profit) / SUM(revenue)，结果按毛利率从高到低排序。"
        return {
            "answer": answer,
            "sql_query": executor.last_sql,
            "data": executor.last_query_result[:50],
            "chart_path": None,
            "tool_trace": executor.tool_trace,
            "rag_sources": executor.rag_sources,
        }

    if "revenue_records" in tables and any(keyword in question for keyword in ["收入", "营收", "趋势"]):
        sql = """
            SELECT
                record_month,
                SUM(revenue) AS total_revenue,
                SUM(cost) AS total_cost,
                SUM(gross_profit) AS total_gross_profit,
                ROUND(SUM(gross_profit) * 1.0 / SUM(revenue), 4) AS gross_margin
            FROM revenue_records
            GROUP BY record_month
            ORDER BY record_month
        """
        await executor.execute("get_schema", {})
        await executor.execute("execute_sql", {"sql": sql})
        answer = f"[模拟回答] 关于「{question}」，系统已按月份汇总 revenue_records 表。\n"
        answer += "从模拟数据看，2024 年收入整体呈上升趋势，年末收入水平高于年初。"
        return {
            "answer": answer,
            "sql_query": executor.last_sql,
            "data": executor.last_query_result[:50],
            "chart_path": None,
            "tool_trace": executor.tool_trace,
            "rag_sources": executor.rag_sources,
        }

    await executor.execute("get_schema", {})
    schema = executor.connector.get_schema()

    answer = f"[模拟回答] 关于「{question}」，这是一个模拟分析结果。\n"
    answer += f"数据库包含 {len(tables)} 个表：{', '.join(tables)}。\n"
    answer += "请配置 API_KEY 以获取真实的 Agent 分析能力。\n\n"

    if schema:
        first_table = schema[0]["table"]
        try:
            preview = executor.connector.preview_table(first_table, 3)
            answer += f"表 {first_table} 预览：\n{json.dumps(preview, ensure_ascii=False, default=str)}\n"
        except Exception:
            pass

    return {
        "answer": answer,
        "sql_query": executor.last_sql,
        "data": executor.last_query_result[:50],
        "chart_path": None,
        "tool_trace": executor.tool_trace,
        "rag_sources": executor.rag_sources,
    }


def save_analysis_record(
    db: Session, question: str, answer: str, sql_query: str,
    data: list[dict], chart_path: str, ds_id: int, user_id: int,
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
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_analysis_records(db: Session, user_id: int, ds_id: int = None) -> list[AnalysisRecord]:
    query = db.query(AnalysisRecord).filter(AnalysisRecord.user_id == user_id)
    if ds_id:
        query = query.filter(AnalysisRecord.ds_id == ds_id)
    return query.order_by(AnalysisRecord.created_at.desc()).all()


def _compact_text(value: str, max_chars: int = 1000) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


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

    if "record_month" in first_row and len(rows) >= 2:
        numeric_fields = [key for key, value in first_row.items() if isinstance(value, (int, float))]
        metric = "total_revenue" if "total_revenue" in first_row else (numeric_fields[0] if numeric_fields else "")
        if metric:
            start = rows[0].get(metric)
            end = rows[-1].get(metric)
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                direction = "上升" if end > start else "下降" if end < start else "基本持平"
                lines.append(f"按月份看，{metric} 从 {start} 变化到 {end}，整体呈{direction}趋势。")

    if "product_line" in first_row and "gross_margin" in first_row:
        top = max(rows, key=lambda row: row.get("gross_margin") or 0)
        lines.append(f"按产品线看，毛利率最高的是 {top.get('product_line')}，毛利率为 {top.get('gross_margin')}。")

    lines.append("你可以继续查看 sql_query、data 和 tool_trace 复核查询口径。")
    return "\n".join(lines)
