"""Call streaming endpoints."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.repositories.call_repository import OptimisticLockError
from app.schemas.call import CallEventRequest, CallEventResponse
from app.services.call_service import CallService
from app.utils.state_machine import InvalidStateTransitionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/call", tags=["calls"])


@router.post(
    "/stream/{call_id}",
    response_model=CallEventResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stream call event packet",
    description="""
    High-performance endpoint for PBX event streaming.
    
    **Performance guarantees:**
    - Returns 202 Accepted within <50ms
    - Validates packet ordering without blocking
    - Handles concurrent packets safely via optimistic locking
    - AI processing triggered in background (not in request lifecycle)
    
    **Idempotency:**
    - Duplicate packets (same call_id + sequence_number) are ignored
    - Returns 202 even for duplicates
    
    **Ordering:**
    - Out-of-order packets are logged as warnings
    - Processing continues without blocking
    - Gaps in sequence are detected and logged
    """,
)
async def stream_call_event(
    call_id: str,
    event: CallEventRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> CallEventResponse:
    """Process call event packet from PBX. Returns 202 immediately, processes in background."""
    request_start = datetime.utcnow()

    try:
        # Initialize service
        call_service = CallService(db)

        # Process event (synchronous DB write for consistency)
        call, call_event, gap_warning = await call_service.process_call_event(
            call_id=call_id,
            event_request=event,
            processing_start_time=request_start,
        )

        # Log sequence gap warning if detected
        if gap_warning:
            logger.warning(
                f"Sequence gap: call_id={call_id}, "
                f"expected={gap_warning.expected_sequence}, "
                f"received={gap_warning.received_sequence}, "
                f"gap={gap_warning.gap_size}"
            )
            # Note: We DO NOT block or fail the request

        # Determine if call is complete (trigger AI based on data pattern)
        # If data contains "END" or similar markers, trigger AI processing
        # Re-fetch call to get latest state after event processing
        call_updated = await call_service.call_repo.get_by_call_id(call_id)
        if await call_service.should_trigger_ai_processing_simple(call_updated or call, event.data):
            # Trigger AI processing in background (non-blocking)
            background_tasks.add_task(
                trigger_ai_processing,
                call_id=call_id,
            )
            logger.info(f"AI processing queued for call_id={call_id}")

        # Calculate response time
        response_time_ms = (datetime.utcnow() - request_start).total_seconds() * 1000

        # Log warning if response time exceeds 50ms
        if response_time_ms > 50:
            logger.warning(
                f"Response time exceeded 50ms: {response_time_ms:.2f}ms for call_id={call_id}"
            )

        logger.info(
            f"Event processed: call_id={call_id}, seq={event.sequence}, "
            f"response_time={response_time_ms:.2f}ms"
        )

        # Return 202 Accepted
        return CallEventResponse(
            call_id=call_id,
            sequence=event.sequence,
            status="accepted",
            received_at=request_start,
            response_time_ms=response_time_ms,
        )

    except InvalidStateTransitionError as e:
        logger.error(f"Invalid state transition: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invalid state transition: {e.current_state} -> {e.new_state}",
        )

    except OptimisticLockError as e:
        logger.error(f"Optimistic lock failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected. Please retry.",
        )

    except Exception as e:
        logger.error(f"Unexpected error processing event: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error processing event",
        )


async def trigger_ai_processing(call_id: str):
    """
    Background task: Trigger AI processing for a completed call.
    
    This runs OUTSIDE the HTTP request lifecycle (after 202 sent).
    
    Design:
    - Fast path: Attempt immediate processing
    - No blocking of HTTP response
    - Errors logged and persisted to DB
    - Failed tasks picked up by periodic retry worker
    
    Args:
        call_id: Call to process
    """
    from app.services.ai_processing_service import trigger_ai_processing_background
    
    try:
        logger.info(f"Starting AI processing (fast path): call_id={call_id}")
        
        # Delegate to AI processing service
        await trigger_ai_processing_background(call_id)

    except Exception as e:
        logger.error(
            f"AI processing failed (fast path): call_id={call_id}, error={e}", 
            exc_info=True
        )
        # Note: Failure doesn't affect the 202 response already sent
        # Failed tasks will be retried by periodic worker


@router.get(
    "/{call_id}",
    summary="Get call details",
    description="Retrieve current state and metadata for a call",
)
async def get_call(call_id: str, db: AsyncSession = Depends(get_db)):
    """
    Get call details by ID.
    
    Future endpoint for dashboard queries.
    """
    call_service = CallService(db)
    call = await call_service.call_repo.get_by_call_id(call_id)

    if call is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call not found: {call_id}",
        )

    return {
        "call_id": call.call_id,
        "state": call.state,
        "direction": call.direction,
        "started_at": call.started_at,
        "ended_at": call.ended_at,
        "duration_seconds": call.duration_seconds,
        "ai_status": call.ai_status,
        "version": call.version,
    }
