"""model endpoint supports images

Revision ID: b4c5d6e7f8a9
Revises: 959c4332745c
Create Date: 2026-08-07 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4c5d6e7f8a9'
# Re-chained onto this repo's own head (the chains diverge in order per repo).
down_revision: Union[str, None] = '959c4332745c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Deliberately nullable with NO default: null means "nobody has checked yet",
    # which is a different state from "this model can't read images" and shows a
    # different tooltip. Existing endpoints start untested, so image attachments
    # stay off until an admin tests or declares them.
    op.add_column('model_endpoint', sa.Column('supports_images', sa.Boolean(), nullable=True))
    op.add_column('model_endpoint',
                  sa.Column('supports_images_source', sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column('model_endpoint', 'supports_images_source')
    op.drop_column('model_endpoint', 'supports_images')
