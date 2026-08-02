import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_repositories
from app.models.models import User
from app.repositories import Repositories
from app.routers.auth import get_current_user_dependency
from app.schemas.schemas import DataSourceCreate, DataSourceResponse, SchemaResponse
from app.services.datasource_service import (
    create_datasource, list_datasources, get_datasource,
    delete_datasource, get_connector_for_ds,
)

logger = logging.getLogger("kb_qa.ds_router")

router = APIRouter(prefix="/api/datasources", tags=["数据源管理"])


@router.get("", response_model=list[DataSourceResponse])
async def get_datasources(
    repositories: Repositories = Depends(get_repositories),
    current_user: User = Depends(get_current_user_dependency),
):
    return list_datasources(repositories.datasources, current_user.id, current_user.shop_id)


@router.post("", response_model=DataSourceResponse)
async def create_ds(
    req: DataSourceCreate,
    repositories: Repositories = Depends(get_repositories),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        ds = create_datasource(
            repositories.datasources,
            req.name,
            req.db_type,
            req.connection_string,
            current_user.id,
            current_user.shop_id,
        )
        return ds
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{ds_id}/schema", response_model=SchemaResponse)
async def get_schema(
    ds_id: int,
    repositories: Repositories = Depends(get_repositories),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        connector = get_connector_for_ds(repositories.datasources, ds_id, current_user.id, current_user.shop_id)
        schema = connector.get_schema()
        return SchemaResponse(tables=schema)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{ds_id}/tables")
async def get_tables(
    ds_id: int,
    repositories: Repositories = Depends(get_repositories),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        connector = get_connector_for_ds(repositories.datasources, ds_id, current_user.id, current_user.shop_id)
        tables = connector.get_tables()
        return {"tables": tables}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{ds_id}/preview/{table_name}")
async def preview_table(
    ds_id: int,
    table_name: str,
    rows: int = Query(default=20, ge=1, le=100),
    repositories: Repositories = Depends(get_repositories),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        connector = get_connector_for_ds(repositories.datasources, ds_id, current_user.id, current_user.shop_id)
        data = connector.preview_table(table_name, rows)
        return {"data": data, "count": len(data)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{ds_id}")
async def remove_ds(
    ds_id: int,
    repositories: Repositories = Depends(get_repositories),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        delete_datasource(repositories.datasources, ds_id, current_user.id, current_user.shop_id)
        return {"code": 0, "message": "删除成功"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
