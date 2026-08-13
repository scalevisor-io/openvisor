"""project git author identity

Revision ID: 3c67fe58f700
Revises: d7e8f9a0b1c2
Create Date: 2026-08-10 21:19:13.383013

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy


revision: str = '3c67fe58f700'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('git_author_name', sa.String(length=120), nullable=True))
    op.add_column('project', sa.Column('git_author_email', sa.String(length=254), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'git_author_email')
    op.drop_column('project', 'git_author_name')
