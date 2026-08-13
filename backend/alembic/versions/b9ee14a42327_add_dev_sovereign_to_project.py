"""add dev_sovereign to project (§Phase 2 sovereign gate)

Revision ID: b9ee14a42327
Revises: 1fb2a180e308
Create Date: 2026-07-13 06:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b9ee14a42327'
down_revision: Union[str, None] = '1fb2a180e308'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_sovereign', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'dev_sovereign')
