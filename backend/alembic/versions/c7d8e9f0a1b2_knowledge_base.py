"""knowledge_base table (multi-KB admin management)

Revision ID: c7d8e9f0a1b2
Revises: 9f1c8376a757
Create Date: 2026-07-11 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = '9f1c8376a757'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'knowledge_base',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('uri', sa.String(length=512), nullable=True),
        sa.Column('api_key_enc', sa.Text(), nullable=True),
        sa.Column('is_removable', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_knowledge_base_kind', 'knowledge_base', ['kind'])
    # At most one built-in row per singleton kind; mcp rows are unconstrained.
    op.create_index(
        'uq_kb_singleton_kind', 'knowledge_base', ['kind'], unique=True,
        postgresql_where=sa.text("kind IN ('local', 'context7')"),
    )


def downgrade() -> None:
    op.drop_index('uq_kb_singleton_kind', table_name='knowledge_base')
    op.drop_index('ix_knowledge_base_kind', table_name='knowledge_base')
    op.drop_table('knowledge_base')
