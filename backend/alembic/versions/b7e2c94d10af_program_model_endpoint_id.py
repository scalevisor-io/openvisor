"""program.model_endpoint_id - saved-endpoint picker replaces the inline trio

Revision ID: b7e2c94d10af
Revises: d51f80a4c793
Create Date: 2026-07-17 10:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2c94d10af'
down_revision: Union[str, None] = 'd51f80a4c793'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('program', sa.Column('model_endpoint_id', sa.String(length=36), nullable=True))
    op.create_foreign_key('fk_program_model_endpoint', 'program', 'model_endpoint',
                          ['model_endpoint_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_program_model_endpoint', 'program', type_='foreignkey')
    op.drop_column('program', 'model_endpoint_id')
