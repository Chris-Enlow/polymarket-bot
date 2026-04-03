"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-02
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leaders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("wallet_address", sa.Text, nullable=False, unique=True),
        sa.Column("win_rate", sa.Numeric(6, 4), nullable=False),
        sa.Column("roi_6m", sa.Numeric(10, 4), nullable=False),
        sa.Column("closed_positions", sa.Integer, nullable=False),
        sa.Column("qualified_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, default=True),
    )

    op.create_table(
        "simulated_trades",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("leader_id", UUID(as_uuid=True), sa.ForeignKey("leaders.id"), nullable=False),
        sa.Column("market_id", sa.Text, nullable=False),
        sa.Column("token_side", sa.Text, nullable=False),
        sa.Column("simulated_price", sa.Numeric(10, 6), nullable=False),
        sa.Column("simulated_size_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolution_outcome", sa.Text, nullable=True),
        sa.Column("pnl_usd", sa.Numeric(12, 4), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="OPEN"),
    )
    op.create_index("ix_simulated_trades_leader_id", "simulated_trades", ["leader_id"])
    op.create_index("ix_simulated_trades_market_id", "simulated_trades", ["market_id"])
    op.create_index("ix_simulated_trades_status", "simulated_trades", ["status"])


def downgrade() -> None:
    op.drop_table("simulated_trades")
    op.drop_table("leaders")
