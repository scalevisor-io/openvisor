"""chat images

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-07 22:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'chat_image',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('project.id'), nullable=False),
        # null between upload and the message that claims it
        sa.Column('message_id', sa.String(36), sa.ForeignKey('message.id'), nullable=True),
        sa.Column('author', sa.String(20), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('content_type', sa.String(64), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_chat_image_project_id', 'chat_image', ['project_id'])
    op.create_index('ix_chat_image_message_id', 'chat_image', ['message_id'])


def downgrade() -> None:
    op.drop_index('ix_chat_image_message_id', table_name='chat_image')
    op.drop_index('ix_chat_image_project_id', table_name='chat_image')
    op.drop_table('chat_image')
