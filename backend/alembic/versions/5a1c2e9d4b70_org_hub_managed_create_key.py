"""organization hub_managed flag + idempotent hub-create key (§hub pass-through)

Revision ID: 5a1c2e9d4b70
Revises: 79399933558d
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5a1c2e9d4b70'
down_revision: Union[str, None] = '79399933558d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('organization', sa.Column('hub_managed', sa.Boolean(),
                                            server_default='false', nullable=False))
    op.add_column('organization', sa.Column('hub_create_key', sa.String(length=128),
                                            nullable=True))
    op.create_unique_constraint('uq_organization_hub_create_key', 'organization',
                                ['hub_create_key'])


def downgrade() -> None:
    op.drop_constraint('uq_organization_hub_create_key', 'organization', type_='unique')
    op.drop_column('organization', 'hub_create_key')
    op.drop_column('organization', 'hub_managed')
