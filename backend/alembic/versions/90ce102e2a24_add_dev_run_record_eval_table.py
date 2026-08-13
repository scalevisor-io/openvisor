"""add dev_run_record eval table

Revision ID: 90ce102e2a24
Revises: 8505f0e365a7
Create Date: 2026-07-13 04:33:13.093292

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '90ce102e2a24'
down_revision: Union[str, None] = '8505f0e365a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dev_run_record',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=True),
        sa.Column('spec_id', sa.String(length=64), nullable=False),
        sa.Column('speciality', sa.String(length=64), nullable=True),
        sa.Column('harness_version', sa.String(length=32), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('final_state', sa.String(length=16), nullable=False),
        sa.Column('boot_result', sa.Boolean(), nullable=True),
        sa.Column('contract_ok', sa.Boolean(), nullable=True),
        sa.Column('ci_status', sa.String(length=16), nullable=True),
        sa.Column('security_blocking', sa.Integer(), nullable=False),
        sa.Column('security_ran', sa.Boolean(), nullable=False),
        sa.Column('leak_blocked', sa.Boolean(), nullable=False),
        sa.Column('leak_scanner_errored', sa.Boolean(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('credits', sa.Float(), nullable=False),
        sa.Column('wall_clock_s', sa.Float(), nullable=False),
        sa.Column('error', sa.String(length=512), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_dev_run_record_harness_version'), 'dev_run_record', ['harness_version'], unique=False)
    op.create_index(op.f('ix_dev_run_record_project_id'), 'dev_run_record', ['project_id'], unique=False)
    op.create_index(op.f('ix_dev_run_record_spec_id'), 'dev_run_record', ['spec_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_dev_run_record_spec_id'), table_name='dev_run_record')
    op.drop_index(op.f('ix_dev_run_record_project_id'), table_name='dev_run_record')
    op.drop_index(op.f('ix_dev_run_record_harness_version'), table_name='dev_run_record')
    op.drop_table('dev_run_record')
