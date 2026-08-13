"""project dev_plan + dev_plan_status

Revision ID: d41f22a90b01
Revises: cb220deb66cc
Create Date: 2026-08-05 08:05:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "d41f22a90b01"
down_revision = "cb220deb66cc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project", sa.Column("dev_plan", sa.Text(), nullable=True))
    op.add_column("project", sa.Column("dev_plan_status", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "dev_plan_status")
    op.drop_column("project", "dev_plan")
