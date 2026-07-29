from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.models import AnalysisRecord, DataSource, User


class UserRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, user_id: int) -> User | None:
        return self.db.query(User).filter(User.id == user_id).first()

    def get_by_username(self, username: str) -> User | None:
        return self.db.query(User).filter(User.username == username).first()

    def add(self, user: User) -> User:
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user


class DataSourceRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, datasource: DataSource) -> DataSource:
        self.db.add(datasource)
        self.db.commit()
        self.db.refresh(datasource)
        return datasource

    def list_for_user(self, user_id: int) -> list[DataSource]:
        return self.db.query(DataSource).filter(DataSource.user_id == user_id).all()

    def get_owned(self, ds_id: int, user_id: int) -> DataSource | None:
        return self.db.query(DataSource).filter(
            DataSource.id == ds_id,
            DataSource.user_id == user_id,
        ).first()

    def delete(self, datasource: DataSource) -> None:
        self.db.delete(datasource)
        self.db.commit()


class AnalysisRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, record: AnalysisRecord) -> AnalysisRecord:
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def list_for_user(self, user_id: int, ds_id: int | None = None) -> list[AnalysisRecord]:
        query = self.db.query(AnalysisRecord).filter(AnalysisRecord.user_id == user_id)
        if ds_id is not None:
            query = query.filter(AnalysisRecord.ds_id == ds_id)
        return query.order_by(AnalysisRecord.created_at.desc()).all()

    def get_owned(self, record_id: int, user_id: int) -> AnalysisRecord | None:
        return self.db.query(AnalysisRecord).filter(
            AnalysisRecord.id == record_id,
            AnalysisRecord.user_id == user_id,
        ).first()


@dataclass(frozen=True)
class Repositories:
    users: UserRepository
    datasources: DataSourceRepository
    analyses: AnalysisRepository

    @classmethod
    def from_session(cls, db: Session) -> "Repositories":
        return cls(
            users=UserRepository(db),
            datasources=DataSourceRepository(db),
            analyses=AnalysisRepository(db),
        )
