"""add project_memory.description

Revision ID: e7f8a9b0c1d2
Revises: 6dffa25cdbf7
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, None] = '6dffa25cdbf7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project_memory', sa.Column('description', sa.Text(),
                                              nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('project_memory', 'description')
