"""tool + project_tool_config (§Tools)

Revision ID: e7a90c14fb22
Revises: d41f22a90b01
Create Date: 2026-08-05 15:40:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "e7a90c14fb22"
down_revision = "d41f22a90b01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("slug", sa.String(length=40), nullable=False, unique=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("tools_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "project_tool_config",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("tool_id", sa.String(length=36), sa.ForeignKey("tool.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "tool_id"),
    )


def downgrade() -> None:
    op.drop_table("project_tool_config")
    op.drop_table("tool")
