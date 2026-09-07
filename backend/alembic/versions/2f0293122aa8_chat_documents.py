"""chat documents

Revision ID: 2f0293122aa8
Revises: 8555f099ca48
Create Date: 2026-09-07 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2f0293122aa8'
down_revision: Union[str, None] = '8555f099ca48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'chat_document',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('project.id'), nullable=False),
        # null between upload and the message that claims it
        sa.Column('message_id', sa.String(36), sa.ForeignKey('message.id'), nullable=True),
        sa.Column('author', sa.String(20), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('content_type', sa.String(128), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        # the text the model reads, extracted once at upload
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('char_count', sa.Integer(), nullable=False),
        sa.Column('truncated', sa.Boolean(), nullable=False),
        sa.Column('pages', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_chat_document_project_id', 'chat_document', ['project_id'])
    op.create_index('ix_chat_document_message_id', 'chat_document', ['message_id'])


def downgrade() -> None:
    op.drop_index('ix_chat_document_message_id', table_name='chat_document')
    op.drop_index('ix_chat_document_project_id', table_name='chat_document')
    op.drop_table('chat_document')
