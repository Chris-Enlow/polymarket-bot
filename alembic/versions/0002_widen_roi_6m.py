"""widen roi_6m to NUMERIC(20,4) to support wallets with >$1M PnL

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "leaders",
        "roi_6m",
        existing_type=sa.Numeric(10, 4),
        type_=sa.Numeric(20, 4),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "leaders",
        "roi_6m",
        existing_type=sa.Numeric(20, 4),
        type_=sa.Numeric(10, 4),
        existing_nullable=False,
    )
