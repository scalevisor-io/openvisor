"""add project.dev_request_id (request-scoped dev runs, §14)

Revision ID: b2d3e4f5a6b7
Revises: a1c2d3e4f5a6
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d3e4f5a6b7'
down_revision: Union[str, None] = 'a1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_request_id', sa.String(length=36),
                                       sa.ForeignKey('request.id'), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'dev_request_id')
