"""Pydantic schemas for call streaming API."""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


class CallEventRequest(BaseModel):
    """
    Request schema for POST /v1/call/stream/{call_id}.
    
    Represents a single packet of audio metadata from the PBX system.
    
    Per specification:
    - sequence: Monotonic sequence number for ordering packets
    - data: Audio metadata as string
    - timestamp: Unix timestamp (seconds since epoch)
    """

    sequence: int = Field(
        ...,
        description="Monotonic sequence number from PBX for ordering packets",
        ge=1,
        examples=[1, 2, 3],
    )

    data: str = Field(
        ...,
        description="Audio metadata string",
        min_length=1,
        examples=["audio_chunk_data_base64_encoded"],
    )

    timestamp: float = Field(
        ...,
        description="Unix timestamp when packet was generated (seconds since epoch)",
        gt=0,
        examples=[1738411815.123],
    )

    class Config:
        json_schema_extra = {
            "example": {
                "sequence": 1,
                "data": "audio_metadata_packet_001",
                "timestamp": 1738411815.123,
            }
        }


class CallEventResponse(BaseModel):
    """
    Response schema for POST /v1/call/stream/{call_id}.
    
    Returns 202 Accepted with minimal data for <50ms response.
    """

    call_id: str = Field(..., description="Unique call identifier")
    sequence: int = Field(..., description="Acknowledged sequence number")
    status: str = Field(default="accepted", description="Processing status")
    received_at: datetime = Field(..., description="Server timestamp when packet received")
    response_time_ms: float = Field(..., description="Response time in milliseconds")

    class Config:
        json_schema_extra = {
            "example": {
                "call_id": "call_abc123",
                "sequence": 5,
                "status": "accepted",
                "received_at": "2026-02-01T10:30:15.123Z",
                "response_time_ms": 12.5,
            }
        }


class SequenceGapWarning(BaseModel):
    """
    Warning model for missing packet sequences.
    
    Used internally for logging, not exposed to API.
    """

    call_id: str
    expected_sequence: int
    received_sequence: int
    gap_size: int
    detected_at: datetime


class CallStateResponse(BaseModel):
    """Response schema for GET /v1/call/{call_id} (future endpoint)."""

    call_id: str
    state: str
    direction: str
    caller_number: Optional[str] = None
    callee_number: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    ai_status: str
    version: int

    class Config:
        from_attributes = True  # Enable ORM mode


class CallCreateRequest(BaseModel):
    """Request schema for initiating a new call (future endpoint)."""

    call_id: str = Field(..., description="Unique external call identifier")
    direction: str = Field(..., description="INBOUND or OUTBOUND")
    caller_number: str = Field(..., description="Caller phone number")
    callee_number: str = Field(..., description="Callee phone number")
    queue_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str) -> str:
        """Ensure direction is valid."""
        v = v.upper()
        if v not in ("INBOUND", "OUTBOUND"):
            raise ValueError("direction must be INBOUND or OUTBOUND")
        return v
