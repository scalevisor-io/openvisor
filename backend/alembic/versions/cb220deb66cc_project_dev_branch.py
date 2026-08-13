"""project dev_branch

Revision ID: cb220deb66cc
Revises: cf88ae79f12b
Create Date: 2026-08-05 07:20:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "cb220deb66cc"
down_revision = "cf88ae79f12b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project", sa.Column("dev_branch", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "dev_branch")
