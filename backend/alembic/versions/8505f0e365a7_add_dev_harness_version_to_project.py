"""add dev_harness_version to project

Revision ID: 8505f0e365a7
Revises: d5e6f7a8b9c0
Create Date: 2026-07-13 02:38:57.022555

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8505f0e365a7'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_harness_version', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'dev_harness_version')
