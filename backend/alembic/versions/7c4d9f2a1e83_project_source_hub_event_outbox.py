"""project source/hub_ref + hub_project_event outbox (§hub pass-through P1)

Revision ID: 7c4d9f2a1e83
Revises: 5a1c2e9d4b70
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c4d9f2a1e83'
down_revision: Union[str, None] = '5a1c2e9d4b70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('source', sa.String(length=16),
                                       server_default='customer', nullable=False))
    op.add_column('project', sa.Column('hub_ref', sa.String(length=64), nullable=True))
    op.create_table(
        'hub_project_event',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36),
                  sa.ForeignKey('project.id'), nullable=False, index=True),
        sa.Column('hub_ref', sa.String(length=64), nullable=True),
        sa.Column('etype', sa.String(length=32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_table('hub_project_event')
    op.drop_column('project', 'hub_ref')
    op.drop_column('project', 'source')
