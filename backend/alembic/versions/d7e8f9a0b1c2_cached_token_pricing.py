"""cached-token pricing (§18): endpoint cached_input_price + ledger tokens_cached

Revision ID: d7e8f9a0b1c2
Revises: c5d6e7f8a9b0
Create Date: 2026-08-10 00:00:00.000000

Prompt-cache READS reported by providers are now billed at a discounted rate:
  - model_endpoint.cached_input_price: admin-supplied per-1M price for cached
    input tokens on custom-priced endpoints (null = no discount)
  - credit_transaction.tokens_cached: the cached subset of a consumption row's
    tokens, for ledger transparency (null = none reported)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('model_endpoint',
                  sa.Column('cached_input_price', sa.Float(), nullable=True))
    op.add_column('credit_transaction',
                  sa.Column('tokens_cached', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('credit_transaction', 'tokens_cached')
    op.drop_column('model_endpoint', 'cached_input_price')
