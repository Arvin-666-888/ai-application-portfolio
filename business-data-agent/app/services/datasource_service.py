import logging

from app.models.models import DataSource
from app.repositories import DataSourceRepository
from app.utils.db_connector import ConnectionManager

logger = logging.getLogger("business_data_agent.ds_service")


def create_datasource(
    datasources: DataSourceRepository,
    name: str,
    db_type: str,
    connection_string: str,
    user_id: int,
    shop_id: str,
) -> DataSource:
    if db_type != "sqlite" or not connection_string.startswith("sqlite:///"):
        raise ValueError("当前版本仅支持 SQLite 数据源")
    ds = datasources.add(DataSource(
        name=name,
        db_type=db_type,
        connection_string=connection_string,
        user_id=user_id,
        shop_id=shop_id,
    ))
    connector = ConnectionManager.get_connector(ds.id, user_id, shop_id, connection_string)
    if not connector.test_connection():
        ConnectionManager.remove_connector(ds.id, user_id, shop_id)
        datasources.delete(ds)
        raise ValueError("数据库连接失败，请检查连接字符串")
    logger.info("DataSource created: %s - %s", ds.id, name)
    return ds


def list_datasources(
    datasources: DataSourceRepository,
    user_id: int,
    shop_id: str,
) -> list[DataSource]:
    return datasources.list_owned(user_id, shop_id)


def get_datasource(
    datasources: DataSourceRepository,
    ds_id: int,
    user_id: int,
    shop_id: str,
) -> DataSource:
    ds = datasources.get_owned(ds_id, user_id, shop_id)
    if not ds:
        raise ValueError("数据源不存在")
    return ds


def delete_datasource(
    datasources: DataSourceRepository,
    ds_id: int,
    user_id: int,
    shop_id: str,
):
    ds = get_datasource(datasources, ds_id, user_id, shop_id)
    ConnectionManager.remove_connector(ds_id, user_id, shop_id)
    datasources.delete(ds)
    logger.info("DataSource %s deleted", ds_id)


def get_connector_for_ds(
    datasources: DataSourceRepository,
    ds_id: int,
    user_id: int,
    shop_id: str,
):
    ds = get_datasource(datasources, ds_id, user_id, shop_id)
    return ConnectionManager.get_connector(ds_id, user_id, shop_id, ds.connection_string)
