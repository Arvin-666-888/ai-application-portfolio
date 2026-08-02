import json
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("shop_id", "username", name="uq_users_shop_username"),
        Index("ix_users_shop_id", "shop_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    datasources: Mapped[list["DataSource"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    analysis_records: Mapped[list["AnalysisRecord"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class DataSource(Base):
    __tablename__ = "datasources"
    __table_args__ = (Index("ix_datasources_owner_shop", "user_id", "shop_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_string: Mapped[str] = mapped_column(String(500), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship(back_populates="datasources")
    analysis_records: Mapped[list["AnalysisRecord"]] = relationship(back_populates="datasource", cascade="all, delete-orphan")


class AnalysisRecord(Base):
    __tablename__ = "analysis_records"
    __table_args__ = (Index("ix_analysis_records_owner_shop", "user_id", "shop_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(1000), nullable=False)
    answer: Mapped[str] = mapped_column(Text, default="")
    sql_query: Mapped[str] = mapped_column(Text, default="")
    query_result: Mapped[str] = mapped_column(Text, default="[]")
    chart_path: Mapped[str] = mapped_column(String(255), default="")
    tool_trace: Mapped[str] = mapped_column(Text, default="[]")
    rag_sources: Mapped[str] = mapped_column(Text, default="[]")
    ds_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship(back_populates="analysis_records")
    datasource: Mapped["DataSource"] = relationship(back_populates="analysis_records")

    @property
    def tool_count(self) -> int:
        try:
            return len(json.loads(self.tool_trace or "[]"))
        except json.JSONDecodeError:
            return 0
