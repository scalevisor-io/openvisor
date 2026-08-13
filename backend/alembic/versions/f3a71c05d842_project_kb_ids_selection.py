"""project kb_ids (§KB per-project selection)

Revision ID: f3a71c05d842
Revises: abd687a8409b
Create Date: 2026-07-16 18:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a71c05d842'
down_revision: Union[str, None] = 'abd687a8409b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('kb_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'kb_ids')
