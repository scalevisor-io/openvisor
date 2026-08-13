"""program_instance.model_endpoint_id - the customer's per-instance model pick

Revision ID: 677c1728c6f2
Revises: ac63c881fc37
Create Date: 2026-08-13 15:54:13.154590

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '677c1728c6f2'
down_revision: Union[str, None] = 'ac63c881fc37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('program_instance',
                  sa.Column('model_endpoint_id', sa.String(length=36), nullable=True))
    op.create_foreign_key('fk_program_instance_model_endpoint', 'program_instance',
                          'model_endpoint', ['model_endpoint_id'], ['id'],
                          ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_program_instance_model_endpoint', 'program_instance',
                       type_='foreignkey')
    op.drop_column('program_instance', 'model_endpoint_id')
