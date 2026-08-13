"""multi-repo: project_repo.provider/is_push_target/auto_merge (move auto_merge off project)

Revision ID: f1a2b3c4d5e6
Revises: e8f9a0b1c2d3
Create Date: 2026-07-10 00:00:00.000000

auto_merge is now per push-repo (§14.7 multi-repo). The move:
  - add project_repo.provider (detected from the URL host), is_push_target, auto_merge
  - backfill: mark the existing primary repo the push target, copy the old
    Project.auto_merge onto it, and detect each repo's provider from its URL
  - drop project.auto_merge (read from the push repo now)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('project_repo', sa.Column('provider', sa.String(length=16),
                                            nullable=False, server_default='other'))
    op.add_column('project_repo', sa.Column('is_push_target', sa.Boolean(),
                                            nullable=False, server_default='false'))
    op.add_column('project_repo', sa.Column('auto_merge', sa.Boolean(),
                                            nullable=False, server_default='false'))
    # provider from the URL host (github > gitlab > other)
    op.execute("UPDATE project_repo SET provider = 'github' WHERE ssh_uri LIKE '%github.com%'")
    op.execute("UPDATE project_repo SET provider = 'gitlab' "
               "WHERE provider = 'other' AND ssh_uri LIKE '%gitlab%'")
    # the existing primary repo becomes the push target
    op.execute("UPDATE project_repo SET is_push_target = true WHERE role = 'primary'")
    # carry the old per-project auto_merge onto its push repo
    op.execute("UPDATE project_repo SET auto_merge = true WHERE is_push_target = true "
               "AND project_id IN (SELECT id FROM project WHERE auto_merge = true)")
    op.drop_column('project', 'auto_merge')


def downgrade() -> None:
    op.add_column('project', sa.Column('auto_merge', sa.Boolean(), nullable=False,
                                       server_default='false'))
    # restore Project.auto_merge from the push repo's flag
    op.execute("UPDATE project SET auto_merge = true WHERE id IN "
               "(SELECT project_id FROM project_repo WHERE is_push_target = true AND auto_merge = true)")
    op.drop_column('project_repo', 'auto_merge')
    op.drop_column('project_repo', 'is_push_target')
    op.drop_column('project_repo', 'provider')
