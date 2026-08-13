"""api token project scope

Revision ID: f2b3c4d5e6a7
Revises: e1a2b3c4d5e6
Create Date: 2026-08-07 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2b3c4d5e6a7'
down_revision: Union[str, None] = 'e1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable by design: every existing user/hub token keeps project_id NULL and
    # authenticates exactly as before (the MCP sidecar's auth query inner-joins
    # "user", so user_id stays required for all scopes).
    op.add_column('api_token', sa.Column('project_id', sa.String(36), nullable=True))
    op.create_foreign_key('fk_api_token_project', 'api_token', 'project',
                          ['project_id'], ['id'])
    op.create_index('ix_api_token_project_id', 'api_token', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_api_token_project_id', table_name='api_token')
    op.drop_constraint('fk_api_token_project', 'api_token', type_='foreignkey')
    op.drop_column('api_token', 'project_id')
