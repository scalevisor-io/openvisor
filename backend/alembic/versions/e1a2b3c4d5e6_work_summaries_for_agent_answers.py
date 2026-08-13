"""work summaries for agent answers

Revision ID: e1a2b3c4d5e6
Revises: 7dd77ff9a94c
Create Date: 2026-08-07 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1a2b3c4d5e6'
down_revision: Union[str, None] = '7dd77ff9a94c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_summary', sa.Text(), nullable=True))
    op.add_column('request', sa.Column('work_summary', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('request', 'work_summary')
    op.drop_column('project', 'dev_summary')
