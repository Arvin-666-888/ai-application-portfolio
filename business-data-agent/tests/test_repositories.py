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


def test_repositories_share_session_and_enforce_ownership():
    repositories, session = _repositories()
    try:
        assert repositories.users.db is session
        assert repositories.datasources.db is session
        assert repositories.analyses.db is session

        first_user = repositories.users.add(User(username="first", hashed_password="hash"))
        second_user = repositories.users.add(User(username="second", hashed_password="hash"))
        datasource = repositories.datasources.add(
            DataSource(
                name="finance",
                db_type="sqlite",
                connection_string="sqlite:///:memory:",
                user_id=first_user.id,
            )
        )

        assert repositories.datasources.get_owned(datasource.id, first_user.id) == datasource
        assert repositories.datasources.get_owned(datasource.id, second_user.id) is None
    finally:
        session.close()


def test_analysis_repository_filters_and_orders_records():
    repositories, session = _repositories()
    try:
        user = repositories.users.add(User(username="analyst", hashed_password="hash"))
        first_source = repositories.datasources.add(
            DataSource(
                name="first",
                db_type="sqlite",
                connection_string="sqlite:///:memory:",
                user_id=user.id,
            )
        )
        second_source = repositories.datasources.add(
            DataSource(
                name="second",
                db_type="sqlite",
                connection_string="sqlite:///:memory:",
                user_id=user.id,
            )
        )
        repositories.analyses.add(
            AnalysisRecord(
                question="older",
                ds_id=first_source.id,
                user_id=user.id,
                created_at=datetime(2026, 1, 1),
            )
        )
        newest = repositories.analyses.add(
            AnalysisRecord(
                question="newest",
                ds_id=second_source.id,
                user_id=user.id,
                created_at=datetime(2026, 1, 2),
            )
        )

        assert [record.question for record in repositories.analyses.list_for_user(user.id)] == [
            "newest",
            "older",
        ]
        assert repositories.analyses.list_for_user(user.id, first_source.id)[0].question == "older"
        assert repositories.analyses.get_owned(newest.id, user.id) == newest
    finally:
        session.close()
