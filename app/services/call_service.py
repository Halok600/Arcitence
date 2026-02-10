"""Call service layer - orchestrates business logic and repository calls."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import Call, CallEvent
from app.repositories.call_repository import (
    CallEventRepository,
    CallRepository,
    OptimisticLockError,
)
from app.schemas.call import CallEventRequest, SequenceGapWarning
from app.utils.state_machine import CallStateMachine, InvalidStateTransitionError
from app.core.websocket_manager import websocket_manager

logger = logging.getLogger(__name__)


class CallService:
    """Handles call event processing with ordering validation and concurrent update handling."""
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self.call_repo = CallRepository(db)
        self.event_repo = CallEventRepository(db)

    async def process_call_event(
        self,
        call_id: str,
        event_request: CallEventRequest,
        processing_start_time: datetime,
    ) -> tuple[Call, CallEvent, Optional[SequenceGapWarning]]:
        """Process call event - validates ordering, handles concurrent updates."""
        # Step 1: Get or create call
        call, created = await self.call_repo.get_or_create_call(
            call_id=call_id, direction="INBOUND"  # Default to INBOUND
        )

        if created:
            logger.info(f"New call created: call_id={call_id}")

        # Step 2: Calculate processing duration
        processing_end_time = datetime.utcnow()
        processing_duration_ms = int(
            (processing_end_time - processing_start_time).total_seconds() * 1000
        )

        # Step 3: Convert timestamp to datetime
        event_timestamp = datetime.fromtimestamp(event_request.timestamp)

        # Step 4: Insert event (idempotent)
        event, was_inserted = await self.event_repo.insert_event(
            call_id=call_id,
            event_type="AUDIO_PACKET",  # Simple type for audio metadata
            sequence_number=event_request.sequence,
            event_timestamp=event_timestamp,
            payload={"data": event_request.data},  # Store data in payload
            previous_state=None,
            new_state=None,
            processing_duration_ms=processing_duration_ms,
        )

        # Broadcast packet ingestion to supervisors (non-blocking)
        if was_inserted:
            try:
                logger.info(f"Broadcasting PACKET_INGESTED for call_id={call_id}")
                result = await websocket_manager.broadcast(
                    event_type="PACKET_INGESTED",
                    call_id=call_id,
                    data={
                        "sequence": event_request.sequence,
                        "data_length": len(event_request.data),
                        "timestamp": event_request.timestamp,
                        "processing_duration_ms": processing_duration_ms,
                    },
                    agent_id=call.agent_id if hasattr(call, 'agent_id') else None
                )
                logger.info(f"Broadcast result: {result} supervisors notified")
            except Exception as e:
                logger.error(f"WebSocket broadcast failed: {e}")

        # Step 5: Detect sequence gaps (non-blocking warning)
        gap_warning = None
        if was_inserted:
            gap_warning = await self._check_sequence_gap(
                call_id, event_request.sequence
            )

        # Step 6: Update call state internally
        # For simple schema, we don't have explicit state transitions in the packet
        # State management happens internally based on call patterns

        return call, event, gap_warning

    async def _check_sequence_gap(
        self, call_id: str, current_sequence: int
    ) -> Optional[SequenceGapWarning]:
        """
        Check for sequence gaps without blocking.
        
        Strategy:
        - Get latest sequence before this one
        - If gap > 0, log warning but continue
        - Return warning for potential alerting
        
        Args:
            call_id: Call identifier
            current_sequence: Sequence number just inserted
            
        Returns:
            SequenceGapWarning if gap detected, None otherwise
        """
        try:
            # Get second-latest sequence (latest is the one we just inserted)
            latest_seq = await self.event_repo.get_latest_sequence(call_id)

            if latest_seq is None:
                # First event, no gap possible
                return None

            expected_sequence = latest_seq + 1

            if current_sequence > expected_sequence:
                gap_size = current_sequence - expected_sequence
                warning = SequenceGapWarning(
                    call_id=call_id,
                    expected_sequence=expected_sequence,
                    received_sequence=current_sequence,
                    gap_size=gap_size,
                    detected_at=datetime.utcnow(),
                )

                logger.warning(
                    f"Sequence gap detected: call_id={call_id}, "
                    f"expected={expected_sequence}, received={current_sequence}, "
                    f"gap_size={gap_size}"
                )

                return warning

            elif current_sequence < latest_seq:
                # Out-of-order but within seen range (late arrival)
                logger.info(
                    f"Out-of-order event: call_id={call_id}, "
                    f"seq={current_sequence}, latest={latest_seq} (late arrival)"
                )

            return None

        except Exception as e:
            # Never block on gap detection failures
            logger.error(f"Error checking sequence gap: {e}", exc_info=True)
            return None

    async def _update_call_state_if_valid(
        self, call: Call, new_state: str, event_type: str
    ) -> Call:
        """
        Update call state if transition is valid.
        
        Uses optimistic locking with automatic retry on conflict.
        
        Args:
            call: Current call object
            new_state: Desired new state
            event_type: Event type triggering the transition
            
        Returns:
            Updated Call object
            
        Raises:
            InvalidStateTransitionError: If transition invalid
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Validate state transition
                CallStateMachine.validate_transition(
                    current_state=call.state,
                    new_state=new_state,
                    call_id=call.call_id,
                )

                # Prepare additional fields based on state
                ended_at = None
                duration_seconds = None

                if new_state in ("COMPLETED", "FAILED"):
                    ended_at = datetime.utcnow()
                    if call.started_at:
                        duration_seconds = int(
                            (ended_at - call.started_at).total_seconds()
                        )

                # Update with optimistic lock
                updated_call = await self.call_repo.update_state(
                    call_id=call.call_id,
                    new_state=new_state,
                    expected_version=call.version,
                    ended_at=ended_at,
                    duration_seconds=duration_seconds,
                )

                logger.info(
                    f"State transition: call_id={call.call_id}, "
                    f"{call.state} -> {new_state}, event={event_type}"
                )
                
                # Broadcast state change to supervisors (non-blocking)
                asyncio.create_task(
                    websocket_manager.broadcast(
                        event_type="STATE_CHANGED",
                        call_id=call.call_id,
                        data={
                            "from_state": call.state,
                            "to_state": new_state,
                            "event_type": event_type,
                        },
                        agent_id=updated_call.agent_id if hasattr(updated_call, 'agent_id') else None
                    )
                )

                return updated_call

            except OptimisticLockError:
                # Concurrent modification - reload and retry
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Optimistic lock conflict (attempt {attempt + 1}), retrying..."
                    )
                    # Reload call with latest version
                    call = await self.call_repo.get_by_call_id(call.call_id)
                    if call is None:
                        raise ValueError(f"Call disappeared: {call.call_id}")
                    continue
                else:
                    logger.error(
                        f"Optimistic lock failed after {max_retries} attempts"
                    )
                    raise

            except InvalidStateTransitionError as e:
                # Log but don't fail the request
                logger.warning(f"Invalid state transition ignored: {e}")
                # Return original call unchanged
                return call

        return call

    async def should_trigger_ai_processing_simple(self, call: Call, data: str) -> bool:
        """
        Determine if AI processing should be triggered based on simple data pattern.
        
        Trigger conditions:
        - Data contains markers indicating call completion (e.g., "END", "COMPLETE")
        - AI not already processing or completed
        - Auto-complete the call and set recording URL if marker found
        
        Args:
            call: Call object
            data: Audio metadata string
            
        Returns:
            True if AI should be triggered
        """
        # Check if data indicates call completion
        data_upper = data.upper()
        completion_markers = ["END", "COMPLETE", "FINISHED", "TERMINATED"]
        
        has_completion_marker = any(marker in data_upper for marker in completion_markers)
        
        if not has_completion_marker:
            return False

        if call.ai_status in ("PROCESSING", "COMPLETED"):
            return False

        # Auto-complete the call if not already completed
        if call.state == "IN_PROGRESS":
            try:
                # Set recording URL (simulated S3 path based on call_id)
                recording_url = f"s3://articence-recordings/{call.call_id}.wav"
                
                # Update call to COMPLETED state with recording URL
                await self.call_repo.update_state(
                    call_id=call.call_id,
                    new_state="COMPLETED",
                    expected_version=call.version,
                    ended_at=datetime.utcnow(),
                    duration_seconds=int((datetime.utcnow() - call.started_at).total_seconds()),
                    recording_url=recording_url,
                )
                await self.db.commit()
                
                logger.info(
                    f"Call auto-completed: call_id={call.call_id}, "
                    f"recording_url={recording_url}"
                )
                
            except Exception as e:
                logger.error(f"Failed to auto-complete call {call.call_id}: {e}")
                return False

        return True

    async def should_trigger_ai_processing(self, call: Call, event_type: str) -> bool:
        """
        Determine if AI processing should be triggered.
        
        Trigger conditions:
        - Call state is COMPLETED
        - Event type is ENDED or COMPLETED
        - AI not already processing or completed
        
        Args:
            call: Call object
            event_type: Event type
            
        Returns:
            True if AI should be triggered
        """
        if call.state != "COMPLETED":
            return False

        if event_type not in ("ENDED", "COMPLETED"):
            return False

        if call.ai_status in ("PROCESSING", "COMPLETED"):
            return False

        return True
