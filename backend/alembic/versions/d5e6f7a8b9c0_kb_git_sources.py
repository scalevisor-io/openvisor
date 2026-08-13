"""git knowledge-base sources (auth_kind, ref, deploy key, verified, ingest status)

Revision ID: d5e6f7a8b9c0
Revises: c7d8e9f0a1b2
Create Date: 2026-07-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('knowledge_base', sa.Column('auth_kind', sa.String(length=8), nullable=True))
    op.add_column('knowledge_base', sa.Column('ref', sa.String(length=128), nullable=True))
    op.add_column('knowledge_base', sa.Column('ssh_public_key', sa.Text(), nullable=True))
    op.add_column('knowledge_base', sa.Column('ssh_private_key_enc', sa.Text(), nullable=True))
    op.add_column('knowledge_base', sa.Column('verified', sa.Boolean(),
                                              server_default=sa.false(), nullable=False))
    op.add_column('knowledge_base', sa.Column('last_indexed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('knowledge_base', sa.Column('last_index_error', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('knowledge_base', 'last_index_error')
    op.drop_column('knowledge_base', 'last_indexed_at')
    op.drop_column('knowledge_base', 'verified')
    op.drop_column('knowledge_base', 'ssh_private_key_enc')
    op.drop_column('knowledge_base', 'ssh_public_key')
    op.drop_column('knowledge_base', 'ref')
    op.drop_column('knowledge_base', 'auth_kind')
