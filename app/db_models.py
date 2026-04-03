"""
SQLAlchemy async ORM models.

Two tables:
  leaders          — qualified copy-trade sources
  simulated_trades — paper positions opened when a leader trades
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship

from app.config import settings


# ---------------------------------------------------------------------------
# Base & engine
# ---------------------------------------------------------------------------

class Base(AsyncAttrs, DeclarativeBase):
    __allow_unmapped__ = True


def build_engine() -> AsyncEngine:
    """Create the async engine with a connection pool tuned for concurrent use."""
    return create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,   # drop stale connections immediately
        echo=False,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Leader(Base):
    __tablename__ = "leaders"

    id: uuid.UUID = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    wallet_address: str = Column(Text, nullable=False, unique=True)
    win_rate: float = Column(Numeric(6, 4), nullable=False)
    roi_6m: float = Column(Numeric(20, 4), nullable=False)
    closed_positions: int = Column(Integer, nullable=False)
    qualified_at: datetime = Column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    # False = demoted; scanner will flip this flag
    active: bool = Column(Boolean, nullable=False, default=True)

    trades: list["SimulatedTrade"] = relationship(
        "SimulatedTrade", back_populates="leader", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Leader {self.wallet_address[:10]}… wr={self.win_rate:.1%}>"


class SimulatedTrade(Base):
    __tablename__ = "simulated_trades"

    id: uuid.UUID = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    leader_id: uuid.UUID = Column(
        UUID(as_uuid=True), ForeignKey("leaders.id"), nullable=False, index=True
    )
    market_id: str = Column(Text, nullable=False, index=True)
    token_side: str = Column(Text, nullable=False)          # "YES" | "NO"
    simulated_price: float = Column(Numeric(10, 6), nullable=False)
    simulated_size_usd: float = Column(Numeric(12, 4), nullable=False)
    opened_at: datetime = Column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    resolved_at: datetime | None = Column(TIMESTAMP(timezone=True), nullable=True)
    resolution_outcome: str | None = Column(Text, nullable=True)  # "YES"|"NO"|"INVALID"
    pnl_usd: float | None = Column(Numeric(12, 4), nullable=True)
    status: str = Column(Text, nullable=False, default="OPEN")     # OPEN|RESOLVED|INVALID

    leader: Leader = relationship("Leader", back_populates="trades")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Trade {self.market_id} {self.token_side} @ {self.simulated_price}>"
