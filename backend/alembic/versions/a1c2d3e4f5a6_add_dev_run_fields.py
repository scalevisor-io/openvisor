"""add dev run observability + fail-safe fields to project

Revision ID: a1c2d3e4f5a6
Revises: 9736f8b68826
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c2d3e4f5a6'
down_revision: Union[str, None] = '9736f8b68826'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_run_state', sa.String(length=16),
                                       nullable=False, server_default='idle'))
    op.add_column('project', sa.Column('dev_run_started_at', sa.DateTime(timezone=True),
                                       nullable=True))
    op.add_column('project', sa.Column('dev_run_log', sa.Text(), nullable=True))
    op.add_column('project', sa.Column('dev_run_error', sa.String(length=512), nullable=True))
    op.add_column('project', sa.Column('dev_pr_number', sa.Integer(), nullable=True))
    op.add_column('project', sa.Column('dev_pr_url', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'dev_pr_url')
    op.drop_column('project', 'dev_pr_number')
    op.drop_column('project', 'dev_run_error')
    op.drop_column('project', 'dev_run_log')
    op.drop_column('project', 'dev_run_started_at')
    op.drop_column('project', 'dev_run_state')
