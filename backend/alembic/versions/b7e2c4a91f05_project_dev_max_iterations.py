"""project dev_max_iterations (per-project agent-iteration cap)

Revision ID: b7e2c4a91f05
Revises: 6d303ab57290
Create Date: 2026-08-06 05:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2c4a91f05'
down_revision: Union[str, None] = '6d303ab57290'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_max_iterations', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'dev_max_iterations')
