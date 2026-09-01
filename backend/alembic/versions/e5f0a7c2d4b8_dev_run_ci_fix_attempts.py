"""dev_run ci_fix_attempts (§14.10 CI watch)

Revision ID: e5f0a7c2d4b8
Revises: a4c81f2e9b70
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f0a7c2d4b8'
down_revision: Union[str, None] = 'a4c81f2e9b70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('dev_run', sa.Column('ci_fix_attempts', sa.Integer(),
                                       nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('dev_run', 'ci_fix_attempts')
