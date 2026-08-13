"""model_endpoint reasoning_effort

Revision ID: f19d3ab2c477
Revises: e7a90c14fb22
Create Date: 2026-08-05 16:55:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "f19d3ab2c477"
down_revision = "e7a90c14fb22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_endpoint",
                  sa.Column("reasoning_effort", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("model_endpoint", "reasoning_effort")
