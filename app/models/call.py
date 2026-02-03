"""SQLAlchemy models for PBX call tracking."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DECIMAL,
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Call(Base):
    """
    Primary call entity with state machine tracking.
    
    Design decisions:
    - `version` field enables optimistic locking for concurrent updates
    - `ai_status` separate from `state` (orthogonal concerns)
    - JSONB `metadata` allows PBX-specific extensions
    """

    __tablename__ = "calls"

    # Primary keys
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)

    # State machine
    state: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Call metadata
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    caller_number: Mapped[Optional[str]] = mapped_column(String(50))
    callee_number: Mapped[Optional[str]] = mapped_column(String(50))
    queue_id: Mapped[Optional[str]] = mapped_column(String(100))
    agent_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)

    # Timing
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, index=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)

    # Recording
    recording_url: Mapped[Optional[str]] = mapped_column(Text)

    # AI processing status
    ai_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="PENDING", server_default="PENDING"
    )
    ai_retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ai_last_retry_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Extensibility
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)

    # Concurrency control
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
        onupdate=text("NOW()"),
    )

    __table_args__ = (
        # Ensure valid state values
        CheckConstraint(
            "state IN ('IN_PROGRESS', 'COMPLETED', 'PROCESSING_AI', 'FAILED', 'ARCHIVED')",
            name="valid_state",
        ),
        # Archived calls must have ended
        CheckConstraint(
            "(state != 'ARCHIVED' OR ended_at IS NOT NULL)",
            name="archived_must_have_ended",
        ),
        # In-progress calls must not have ended
        CheckConstraint(
            "(state != 'IN_PROGRESS' OR ended_at IS NULL)",
            name="in_progress_not_ended",
        ),
        # Partial index for active calls only (dashboard queries)
        Index(
            "idx_calls_active_states",
            "state",
            postgresql_where=text("state IN ('IN_PROGRESS', 'PROCESSING_AI')"),
        ),
        # Partial index for AI retry queries
        Index(
            "idx_calls_ai_retry",
            "ai_status",
            "ai_last_retry_at",
            postgresql_where=text("ai_status = 'FAILED' AND ai_retry_count < 5"),
        ),
        # GIN index for JSONB metadata queries
        Index("idx_calls_metadata_gin", "metadata", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<Call(call_id={self.call_id}, state={self.state})>"


class CallEvent(Base):
    """
    Event sourcing table for packet sequence tracking.
    
    Design decisions:
    - Unique constraint on (call_id, sequence_number) for idempotency
    - Separate PBX timestamp vs. our receive timestamp (clock skew)
    - Track processing duration for observability
    """

    __tablename__ = "call_events"

    # Primary key
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Foreign key to calls
    call_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Event details
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # State transition tracking
    previous_state: Mapped[Optional[str]] = mapped_column(String(50))
    new_state: Mapped[Optional[str]] = mapped_column(String(50))

    # Event payload
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Timing (three different timestamps for debugging)
    event_timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )  # PBX's clock
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )  # Our clock
    processed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )  # After DB write

    # Observability
    processing_duration_ms: Mapped[Optional[int]] = mapped_column(Integer)

    __table_args__ = (
        # Idempotency: prevent duplicate events
        Index(
            "idx_events_call_sequence_unique",
            "call_id",
            "sequence_number",
            unique=True,
        ),
        # Query events by call and time
        Index("idx_events_call_id_time", "call_id", "event_timestamp"),
        # Find recent events across all calls
        Index("idx_events_received_at", "received_at"),
    )

    def __repr__(self) -> str:
        return f"<CallEvent(call_id={self.call_id}, seq={self.sequence_number}, type={self.event_type})>"


class AIResult(Base):
    """
    AI processing results storage.
    
    Separated from calls table for:
    - Multiple results per call (sentiment, transcription, scoring)
    - Independent retry tracking per analysis type
    - Clean schema evolution
    """

    __tablename__ = "ai_results"

    # Primary key
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Foreign key to calls
    call_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Analysis details
    analysis_type: Mapped[str] = mapped_column(String(50), nullable=False)
    result_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence_score: Mapped[Optional[float]] = mapped_column(DECIMAL(5, 4))

    # Performance tracking
    processing_started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    processing_completed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    ai_service_duration_ms: Mapped[Optional[int]] = mapped_column(Integer)

    # Error tracking
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )

    __table_args__ = (
        # Find results by analysis type
        Index("idx_ai_type_call", "analysis_type", "call_id"),
        # Time-based queries
        Index("idx_ai_completed_at", "processing_completed_at"),
    )

    def __repr__(self) -> str:
        return f"<AIResult(call_id={self.call_id}, type={self.analysis_type})>"
