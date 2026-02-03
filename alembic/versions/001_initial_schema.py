"""Alembic migration script for initial schema.

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-02-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial schema."""
    
    # Create calls table
    op.create_table(
        'calls',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('call_id', sa.String(length=255), nullable=False),
        sa.Column('state', sa.String(length=50), nullable=False),
        sa.Column('direction', sa.String(length=20), nullable=False),
        sa.Column('caller_number', sa.String(length=50), nullable=True),
        sa.Column('callee_number', sa.String(length=50), nullable=True),
        sa.Column('queue_id', sa.String(length=100), nullable=True),
        sa.Column('agent_id', sa.String(length=100), nullable=True),
        sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('recording_url', sa.Text(), nullable=True),
        sa.Column('ai_status', sa.String(length=50), server_default='PENDING', nullable=False),
        sa.Column('ai_retry_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('ai_last_retry_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.CheckConstraint("state IN ('IN_PROGRESS', 'COMPLETED', 'PROCESSING_AI', 'FAILED', 'ARCHIVED')", name='valid_state'),
        sa.CheckConstraint("(state != 'ARCHIVED' OR ended_at IS NOT NULL)", name='archived_must_have_ended'),
        sa.CheckConstraint("(state != 'IN_PROGRESS' OR ended_at IS NULL)", name='in_progress_not_ended'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Indexes for calls
    op.create_index('idx_calls_call_id', 'calls', ['call_id'], unique=True)
    op.create_index('idx_calls_state', 'calls', ['state'])
    op.create_index('idx_calls_started_at', 'calls', ['started_at'])
    op.create_index('idx_calls_agent_id', 'calls', ['agent_id'])
    op.create_index(
        'idx_calls_active_states', 'calls', ['state'],
        postgresql_where=sa.text("state IN ('IN_PROGRESS', 'PROCESSING_AI')")
    )
    op.create_index(
        'idx_calls_ai_retry', 'calls', ['ai_status', 'ai_last_retry_at'],
        postgresql_where=sa.text("ai_status = 'FAILED' AND ai_retry_count < 5")
    )
    op.create_index('idx_calls_metadata_gin', 'calls', ['metadata'], postgresql_using='gin')
    
    # Create call_events table
    op.create_table(
        'call_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('call_id', sa.String(length=255), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('sequence_number', sa.BigInteger(), nullable=False),
        sa.Column('previous_state', sa.String(length=50), nullable=True),
        sa.Column('new_state', sa.String(length=50), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('event_timestamp', postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('received_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('processed_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('processing_duration_ms', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Indexes for call_events
    op.create_index('idx_events_call_id', 'call_events', ['call_id'])
    op.create_index('idx_events_event_type', 'call_events', ['event_type'])
    op.create_index('idx_events_call_sequence_unique', 'call_events', ['call_id', 'sequence_number'], unique=True)
    op.create_index('idx_events_call_id_time', 'call_events', ['call_id', 'event_timestamp'])
    op.create_index('idx_events_received_at', 'call_events', ['received_at'])
    
    # Create ai_results table
    op.create_table(
        'ai_results',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('call_id', sa.String(length=255), nullable=False),
        sa.Column('analysis_type', sa.String(length=50), nullable=False),
        sa.Column('result_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('confidence_score', sa.DECIMAL(precision=5, scale=4), nullable=True),
        sa.Column('processing_started_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('processing_completed_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('ai_service_duration_ms', sa.Integer(), nullable=True),
        sa.Column('retry_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Indexes for ai_results
    op.create_index('idx_ai_call_id', 'ai_results', ['call_id'])
    op.create_index('idx_ai_type_call', 'ai_results', ['analysis_type', 'call_id'])
    op.create_index('idx_ai_completed_at', 'ai_results', ['processing_completed_at'])


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table('ai_results')
    op.drop_table('call_events')
    op.drop_table('calls')
