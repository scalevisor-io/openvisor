"""add dev_sbom to project (§Phase 2 DevSecOps SBOM/CVE gate)

Revision ID: 79399933558d
Revises: b9ee14a42327
Create Date: 2026-07-13 09:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '79399933558d'
down_revision: Union[str, None] = 'b9ee14a42327'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project', sa.Column('dev_sbom', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'dev_sbom')
