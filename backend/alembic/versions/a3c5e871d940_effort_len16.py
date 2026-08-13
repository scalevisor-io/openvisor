"""widen model_endpoint.reasoning_effort to 16 (custom tiers)

Revision ID: a3c5e871d940
Revises: f19d3ab2c477
Create Date: 2026-08-05 17:55:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "a3c5e871d940"
down_revision = "f19d3ab2c477"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("model_endpoint", "reasoning_effort",
                    type_=sa.String(length=16), existing_type=sa.String(length=8))


def downgrade() -> None:
    op.alter_column("model_endpoint", "reasoning_effort",
                    type_=sa.String(length=8), existing_type=sa.String(length=16))
