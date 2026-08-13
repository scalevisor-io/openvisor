"""knowledge_base http_username

Revision ID: d424d83fdca0
Revises: a3c5e871d940
Create Date: 2026-08-05 21:16:41.833771

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd424d83fdca0'
down_revision: Union[str, None] = 'a3c5e871d940'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('knowledge_base', sa.Column('http_username', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('knowledge_base', 'http_username')
