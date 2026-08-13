"""credit transaction tokens

Revision ID: a3c4d5e6f7b8
Revises: f2b3c4d5e6a7
Create Date: 2026-08-07 19:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c4d5e6f7b8'
down_revision: Union[str, None] = 'f2b3c4d5e6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable: rows written before this (and every non-model row - topup, quote,
    # grant) simply have no token count, and the usage series treats them as 0.
    op.add_column('credit_transaction', sa.Column('tokens', sa.Integer(), nullable=True))
    op.create_index('ix_credit_transaction_created_at', 'credit_transaction', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_credit_transaction_created_at', table_name='credit_transaction')
    op.drop_column('credit_transaction', 'tokens')
