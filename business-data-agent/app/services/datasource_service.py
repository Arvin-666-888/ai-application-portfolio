import logging

from sqlalchemy.orm import Session

from app.models.models import DataSource, User
from app.utils.db_connector import ConnectionManager

logger = logging.getLogger("kb_qa.ds_service")


def create_datasource(db: Session, name: str, db_type: str, connection_string: str, user_id: int) -> DataSource:
    ds = DataSource(name=name, db_type=db_type, connection_string=connection_string, user_id=user_id)
    db.add(ds)
    db.commit()
    db.refresh(ds)

    connector = ConnectionManager.get_connector(ds.id, connection_string)
    if not connector.test_connection():
        db.delete(ds)
        db.commit()
        raise ValueError("数据库连接失败，请检查连接字符串")

    logger.info(f"DataSource created: {ds.id} - {name}")
    return ds


def list_datasources(db: Session, user_id: int) -> list[DataSource]:
    return db.query(DataSource).filter(DataSource.user_id == user_id).all()


def get_datasource(db: Session, ds_id: int, user_id: int) -> DataSource:
    ds = db.query(DataSource).filter(DataSource.id == ds_id).first()
    if not ds:
        raise ValueError("数据源不存在")
    if ds.user_id != user_id:
        raise ValueError("无权访问此数据源")
    return ds


def delete_datasource(db: Session, ds_id: int, user_id: int):
    ds = get_datasource(db, ds_id, user_id)
    ConnectionManager.remove_connector(ds_id)
    db.delete(ds)
    db.commit()
    logger.info(f"DataSource {ds_id} deleted")


def get_connector_for_ds(db: Session, ds_id: int, user_id: int):
    ds = get_datasource(db, ds_id, user_id)
    return ConnectionManager.get_connector(ds_id, ds.connection_string)
