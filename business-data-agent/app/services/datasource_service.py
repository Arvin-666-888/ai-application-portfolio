import logging

from app.models.models import DataSource
from app.repositories import DataSourceRepository
from app.utils.db_connector import ConnectionManager

logger = logging.getLogger("kb_qa.ds_service")


def create_datasource(
    datasources: DataSourceRepository,
    name: str,
    db_type: str,
    connection_string: str,
    user_id: int,
) -> DataSource:
    ds = datasources.add(
        DataSource(name=name, db_type=db_type, connection_string=connection_string, user_id=user_id)
    )

    connector = ConnectionManager.get_connector(ds.id, connection_string)
    if not connector.test_connection():
        datasources.delete(ds)
        raise ValueError("数据库连接失败，请检查连接字符串")

    logger.info(f"DataSource created: {ds.id} - {name}")
    return ds


def list_datasources(datasources: DataSourceRepository, user_id: int) -> list[DataSource]:
    return datasources.list_for_user(user_id)


def get_datasource(datasources: DataSourceRepository, ds_id: int, user_id: int) -> DataSource:
    ds = datasources.get_owned(ds_id, user_id)
    if not ds:
        raise ValueError("数据源不存在")
    return ds


def delete_datasource(datasources: DataSourceRepository, ds_id: int, user_id: int):
    ds = get_datasource(datasources, ds_id, user_id)
    ConnectionManager.remove_connector(ds_id)
    datasources.delete(ds)
    logger.info(f"DataSource {ds_id} deleted")


def get_connector_for_ds(datasources: DataSourceRepository, ds_id: int, user_id: int):
    ds = get_datasource(datasources, ds_id, user_id)
    return ConnectionManager.get_connector(ds_id, ds.connection_string)
