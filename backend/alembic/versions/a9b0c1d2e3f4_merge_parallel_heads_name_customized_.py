"""merge parallel heads (name_customized + user/org profile fields)

The project name_customized flag (407139ba880e) and the user full-name /
org address fields revision (93b8413aea41) were authored in parallel from
3906c156dc89 on two dev instances; this empty merge revision gives alembic
a single head again.

Revision ID: a9b0c1d2e3f4
Revises: 407139ba880e, 93b8413aea41
Create Date: 2026-07-08 00:00:00.000000

"""
from typing import Sequence, Union


revision: str = 'a9b0c1d2e3f4'
down_revision: Union[str, Sequence[str], None] = ('407139ba880e', '93b8413aea41')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
