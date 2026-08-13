"""add per-request token/cost usage counters

Revision ID: c3d4e5f6a7b8
Revises: b2d3e4f5a6b7
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('request', sa.Column('tokens_consumed', sa.Integer(),
                                       nullable=False, server_default='0'))
    op.add_column('request', sa.Column('cost_credits', sa.Float(),
                                       nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('request', 'cost_credits')
    op.drop_column('request', 'tokens_consumed')
