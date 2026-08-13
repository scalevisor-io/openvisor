"""add auto_merge + dev_security_review to project (§14.7 GitHub auto-merge)

Revision ID: e8f9a0b1c2d3
Revises: 3c48e382753c
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, None] = '3c48e382753c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('auto_merge', sa.Boolean(), nullable=False,
                                       server_default='false'))
    op.add_column('project', sa.Column('dev_security_review', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'dev_security_review')
    op.drop_column('project', 'auto_merge')
