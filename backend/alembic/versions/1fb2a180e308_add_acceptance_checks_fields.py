"""add acceptance checks fields (§Phase 1 #5)

Revision ID: 1fb2a180e308
Revises: 90ce102e2a24
Create Date: 2026-07-13 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1fb2a180e308'
down_revision: Union[str, None] = '90ce102e2a24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_acceptance', sa.JSON(), nullable=True))
    op.add_column('dev_run_record', sa.Column('acceptance_passed', sa.Integer(), nullable=True))
    op.add_column('dev_run_record', sa.Column('acceptance_total', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('dev_run_record', 'acceptance_total')
    op.drop_column('dev_run_record', 'acceptance_passed')
    op.drop_column('project', 'dev_acceptance')
