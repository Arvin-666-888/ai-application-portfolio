import io
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.routers.auth import get_current_user_dependency
from app.schemas.schemas import AnalysisRequest, AnalysisResponse, AnalysisRecordResponse
from app.services.agent_service import run_agent, save_analysis_record, get_analysis_records
from app.services.datasource_service import get_connector_for_ds

logger = logging.getLogger("kb_qa.analysis_router")

router = APIRouter(prefix="/api/analysis", tags=["智能分析"])


@router.post("/ask", response_model=AnalysisResponse)
async def ask_question(
    req: AnalysisRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        connector = get_connector_for_ds(db, req.ds_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    try:
        result = await run_agent(req.question, connector)

        record = save_analysis_record(
            db=db,
            question=req.question,
            answer=result["answer"],
            sql_query=result.get("sql_query", ""),
            data=result.get("data", []),
            chart_path=result.get("chart_path", ""),
            tool_trace=result.get("tool_trace", []),
            rag_sources=result.get("rag_sources", []),
            ds_id=req.ds_id,
            user_id=current_user.id,
        )

        return _to_analysis_response(record)
    except Exception as e:
        logger.error(f"Analysis failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")


@router.get("/records", response_model=list[AnalysisRecordResponse])
async def get_records(
    ds_id: int = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    records = get_analysis_records(db, current_user.id, ds_id)
    return records


@router.get("/records/{record_id}", response_model=AnalysisResponse)
async def get_record_detail(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    from app.models.models import AnalysisRecord
    record = db.query(AnalysisRecord).filter(
        AnalysisRecord.id == record_id,
        AnalysisRecord.user_id == current_user.id,
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="分析记录不存在")

    return _to_analysis_response(record)


@router.get("/export/csv/{record_id}")
async def export_csv(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    import csv

    from app.models.models import AnalysisRecord
    record = db.query(AnalysisRecord).filter(
        AnalysisRecord.id == record_id,
        AnalysisRecord.user_id == current_user.id,
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="分析记录不存在")

    data = json.loads(record.query_result) if record.query_result else []
    if not data:
        raise HTTPException(status_code=400, detail="无数据可导出")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys())
    writer.writeheader()
    writer.writerows(data)

    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=analysis_{record_id}.csv"},
    )


@router.get("/export/report/{record_id}")
async def export_report(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    from app.models.models import AnalysisRecord
    record = db.query(AnalysisRecord).filter(
        AnalysisRecord.id == record_id,
        AnalysisRecord.user_id == current_user.id,
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="分析记录不存在")

    data = json.loads(record.query_result) if record.query_result else []

    report = f"# 数据分析报告\n\n"
    report += f"## 分析问题\n{record.question}\n\n"
    report += f"## 分析结论\n{record.answer}\n\n"
    report += f"## 查询语句\n```sql\n{record.sql_query}\n```\n\n"

    if data:
        headers = list(data[0].keys())
        report += "## 查询结果\n\n"
        report += "| " + " | ".join(headers) + " |\n"
        report += "| " + " | ".join(["---"] * len(headers)) + " |\n"
        for row in data[:20]:
            report += "| " + " | ".join(str(v) for v in row.values()) + " |\n"
        if len(data) > 20:
            report += f"\n> 共 {len(data)} 行数据，仅显示前 20 行\n"

    if record.chart_path:
        report += f"\n## 可视化图表\n![图表](/charts/{record.chart_path})\n"

    tool_trace = _loads_json(record.tool_trace, [])
    if tool_trace:
        report += "\n## Agent 工具调用轨迹\n\n"
        report += "| 步骤 | 工具 | 参数 | 结果摘要 |\n"
        report += "| --- | --- | --- | --- |\n"
        for item in tool_trace:
            arguments = json.dumps(item.get("arguments", {}), ensure_ascii=False)
            report += (
                f"| {item.get('step', '')} "
                f"| {item.get('tool', '')} "
                f"| `{_md_cell(arguments)}` "
                f"| {_md_cell(item.get('result_preview', ''))} |\n"
            )

    rag_sources = _loads_json(record.rag_sources, [])
    if rag_sources:
        report += "\n## 文档依据来源\n\n"
        for source in rag_sources[:10]:
            report += f"- {source}\n"

    return StreamingResponse(
        io.BytesIO(report.encode("utf-8")),
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=report_{record_id}.md"},
    )


def _to_analysis_response(record) -> AnalysisResponse:
    return AnalysisResponse(
        id=record.id,
        question=record.question,
        answer=record.answer,
        sql_query=record.sql_query,
        chart_path=record.chart_path or None,
        data=_loads_json(record.query_result, []),
        tool_trace=_loads_json(record.tool_trace, []),
        rag_sources=_loads_json(record.rag_sources, []),
        created_at=record.created_at,
    )


def _loads_json(raw: str, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _md_cell(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "/")
