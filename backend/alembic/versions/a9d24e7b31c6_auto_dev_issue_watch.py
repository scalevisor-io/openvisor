"""auto_dev: project issue_watch + request source issue

Revision ID: a9d24e7b31c6
Revises: f3a71c05d842
Create Date: 2026-07-16 21:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9d24e7b31c6'
down_revision: Union[str, None] = 'f3a71c05d842'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('issue_watch', sa.JSON(), nullable=True))
    op.add_column('request', sa.Column('source_issue_iid', sa.Integer(), nullable=True))
    op.add_column('request', sa.Column('source_issue_url', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('request', 'source_issue_url')
    op.drop_column('request', 'source_issue_iid')
    op.drop_column('project', 'issue_watch')
