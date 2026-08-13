"""project name_customized flag

Revision ID: 407139ba880e
Revises: 3906c156dc89
Create Date: 2026-07-08 01:26:39.113093

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy


revision: str = '407139ba880e'
down_revision: Union[str, None] = '3906c156dc89'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('name_customized', sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
    # Pre-existing projects were named by their customers (the wizard used to
    # ask for a name): mark them customized so the evaluation-time LLM title
    # pass never overwrites a name a human chose.
    op.execute("UPDATE project SET name_customized = true")


def downgrade() -> None:
    op.drop_column('project', 'name_customized')
