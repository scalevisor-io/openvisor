"""program instance inbound trigger hooks (§28)

Revision ID: d51f80a4c793
Revises: a9d24e7b31c6
Create Date: 2026-07-16 23:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd51f80a4c793'
down_revision: Union[str, None] = 'a9d24e7b31c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('program_instance', sa.Column('hook_enabled', sa.Boolean(),
                                                nullable=False, server_default=sa.false()))
    op.add_column('program_instance', sa.Column('hook_secret_enc', sa.Text(), nullable=True))
    op.add_column('program_instance', sa.Column('hook_filters', sa.JSON(), nullable=True))
    op.add_column('program_run', sa.Column('hook_event', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('program_run', 'hook_event')
    op.drop_column('program_instance', 'hook_filters')
    op.drop_column('program_instance', 'hook_secret_enc')
    op.drop_column('program_instance', 'hook_enabled')
