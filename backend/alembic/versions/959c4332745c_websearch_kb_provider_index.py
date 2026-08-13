"""websearch KB rows: one per provider (partial-unique on uri)

Revision ID: 959c4332745c
Revises: a3c4d5e6f7b8
Create Date: 2026-08-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '959c4332745c'
down_revision: Union[str, None] = 'a3c4d5e6f7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Belt-and-braces against a double seed racing across containers: at most one
    # websearch row per provider (uri holds the provider slug for this kind).
    op.create_index("uq_kb_websearch_provider", "knowledge_base", ["uri"],
                    unique=True, postgresql_where=sa.text("kind = 'websearch'"))


def downgrade() -> None:
    op.drop_index("uq_kb_websearch_provider", table_name="knowledge_base")
