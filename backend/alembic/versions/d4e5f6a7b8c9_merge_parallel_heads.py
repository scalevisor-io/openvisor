"""merge parallel heads (request usage + quote details)

The request-usage counters (c3d4e5f6a7b8) and the quote details/pricing
revision (84b2d70a85e7) were authored in parallel from b2d3e4f5a6b7 on the two
dev instances; this empty merge revision gives alembic a single head again.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8, 84b2d70a85e7
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = ('c3d4e5f6a7b8', '84b2d70a85e7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
