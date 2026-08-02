from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.models import AnalysisRecord, DataSource, User
from app.repositories import Repositories


def _repositories() -> tuple[Repositories, Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    return Repositories.from_session(session), session


def test_repositories_enforce_user_and_shop_ownership():
    repositories, session = _repositories()
    try:
        first = repositories.users.add(User(shop_id="amazon-us", username="operator", hashed_password="hash"))
        second = repositories.users.add(User(shop_id="tiktok-uk", username="operator", hashed_password="hash"))
        datasource = repositories.datasources.add(DataSource(
            name="ecommerce",
            db_type="sqlite",
            connection_string="sqlite:///:memory:",
            user_id=first.id,
            shop_id=first.shop_id,
        ))

        assert repositories.users.get_by_username("operator", "amazon-us") == first
        assert repositories.users.get_by_username("operator", "tiktok-uk") == second
        assert repositories.datasources.get_owned(datasource.id, first.id, "amazon-us") == datasource
        assert repositories.datasources.get_owned(datasource.id, first.id, "tiktok-uk") is None
        assert repositories.datasources.get_owned(datasource.id, second.id, "amazon-us") is None
    finally:
        session.close()


def test_analysis_repository_filters_user_shop_and_datasource():
    repositories, session = _repositories()
    try:
        user = repositories.users.add(User(shop_id="amazon-us", username="analyst", hashed_password="hash"))
        source = repositories.datasources.add(DataSource(
            name="source",
            db_type="sqlite",
            connection_string="sqlite:///:memory:",
            user_id=user.id,
            shop_id=user.shop_id,
        ))
        record = repositories.analyses.add(AnalysisRecord(
            question="ROAS",
            ds_id=source.id,
            user_id=user.id,
            shop_id=user.shop_id,
            created_at=datetime(2026, 1, 1),
        ))

        assert repositories.analyses.list_owned(user.id, "amazon-us", source.id) == [record]
        assert repositories.analyses.list_owned(user.id, "tiktok-uk") == []
        assert repositories.analyses.get_owned(record.id, user.id, "amazon-us") == record
        assert repositories.analyses.get_owned(record.id, user.id, "shopee-sg") is None
    finally:
        session.close()
