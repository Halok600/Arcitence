"""Repository layer for database access with optimistic locking.

Design principles:
- Single responsibility: only database operations
- Async/await for non-blocking I/O
- Optimistic locking for concurrent safety
- Transaction management at service layer
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import Call, CallEvent
from app.utils.state_machine import CallStateMachine, InvalidStateTransitionError

logger = logging.getLogger(__name__)


class OptimisticLockError(Exception):
    """Raised when optimistic lock fails (concurrent modification detected)."""

    def __init__(self, call_id: str, expected_version: int):
        self.call_id = call_id
        self.expected_version = expected_version
        super().__init__(
            f"Optimistic lock failed for call {call_id}: "
            f"expected version {expected_version}"
        )


class DuplicateEventError(Exception):
    """Raised when attempting to insert a duplicate event (idempotency violation)."""

    def __init__(self, call_id: str, sequence_number: int):
        self.call_id = call_id
        self.sequence_number = sequence_number
        super().__init__(
            f"Event already exists: call_id={call_id}, sequence={sequence_number}"
        )


class CallRepository:
    """
    Repository for Call entity operations.
    
    Handles:
    - CRUD operations with optimistic locking
    - State transitions with validation
    - Concurrent modification detection
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_call_id(self, call_id: str) -> Optional[Call]:
        """
        Fetch a call by its external call_id.
        
        Args:
            call_id: External PBX call identifier
            
        Returns:
            Call object or None if not found
        """
        stmt = select(Call).where(Call.call_id == call_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_call(
        self,
        call_id: str,
        direction: str,
        caller_number: Optional[str] = None,
        callee_number: Optional[str] = None,
        queue_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Call:
        """
        Create a new call record.
        
        Args:
            call_id: External PBX call identifier
            direction: INBOUND or OUTBOUND
            caller_number: Caller phone number
            callee_number: Callee phone number
            queue_id: Queue identifier
            metadata: PBX-specific metadata
            
        Returns:
            Created Call object
        """
        call = Call(
            call_id=call_id,
            state="IN_PROGRESS",
            direction=direction,
            caller_number=caller_number,
            callee_number=callee_number,
            queue_id=queue_id,
            metadata_=metadata,
            started_at=datetime.utcnow(),
            ai_status="PENDING",
            version=1,
        )
        self.db.add(call)
        await self.db.flush()  # Get the ID without committing
        return call

    async def update_state(
        self,
        call_id: str,
        new_state: str,
        expected_version: int,
        ended_at: Optional[datetime] = None,
        duration_seconds: Optional[int] = None,
        recording_url: Optional[str] = None,
    ) -> Call:
        """
        Update call state with optimistic locking.
        
        This is the critical method for concurrency safety.
        
        Args:
            call_id: External call identifier
            new_state: Desired new state
            expected_version: Version number for optimistic locking
            ended_at: When call ended (if applicable)
            duration_seconds: Call duration (if applicable)
            recording_url: Recording URL (if applicable)
            
        Returns:
            Updated Call object
            
        Raises:
            OptimisticLockError: If version mismatch (concurrent modification)
            InvalidStateTransitionError: If state transition invalid
        """
        # Build update values
        values = {
            "state": new_state,
            "version": expected_version + 1,
            "updated_at": datetime.utcnow(),
        }

        if ended_at is not None:
            values["ended_at"] = ended_at
        if duration_seconds is not None:
            values["duration_seconds"] = duration_seconds
        if recording_url is not None:
            values["recording_url"] = recording_url

        # Execute update with version check
        stmt = (
            update(Call)
            .where(Call.call_id == call_id, Call.version == expected_version)
            .values(**values)
            .returning(Call)
        )

        result = await self.db.execute(stmt)
        updated_call = result.scalar_one_or_none()

        if updated_call is None:
            # Version mismatch or call not found
            raise OptimisticLockError(call_id, expected_version)

        logger.info(
            f"State updated: call_id={call_id}, new_state={new_state}, "
            f"version={expected_version} -> {expected_version + 1}"
        )

        return updated_call

    async def get_or_create_call(
        self, call_id: str, direction: str = "INBOUND"
    ) -> tuple[Call, bool]:
        """
        Get existing call or create if not exists.
        
        Handles race conditions where two concurrent requests try to create
        the same call by catching IntegrityError and retrying the get.
        
        Returns:
            Tuple of (Call, created: bool)
        """
        from sqlalchemy.exc import IntegrityError
        
        call = await self.get_by_call_id(call_id)
        if call is not None:
            return call, False

        try:
            # Create new call
            call = await self.create_call(call_id=call_id, direction=direction)
            return call, True
        except IntegrityError:
            # Race condition: another request created the call
            # Roll back and try to get it again
            await self.db.rollback()
            call = await self.get_by_call_id(call_id)
            if call is None:
                # Should never happen, but handle gracefully
                raise RuntimeError(f"Failed to get or create call: {call_id}")
            return call, False


class CallEventRepository:
    """
    Repository for CallEvent entity operations.
    
    Handles:
    - Event insertion with idempotency
    - Sequence gap detection
    - Event history queries
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def insert_event(
        self,
        call_id: str,
        event_type: str,
        sequence_number: int,
        event_timestamp: datetime,
        payload: dict,
        previous_state: Optional[str] = None,
        new_state: Optional[str] = None,
        processing_duration_ms: Optional[int] = None,
    ) -> tuple[CallEvent, bool]:
        """
        Insert event with idempotency (ON CONFLICT DO NOTHING).
        
        Args:
            call_id: Call identifier
            event_type: Type of event
            sequence_number: PBX sequence number
            event_timestamp: PBX timestamp
            payload: Event data
            previous_state: State before event
            new_state: State after event
            processing_duration_ms: Processing time
            
        Returns:
            Tuple of (CallEvent, was_inserted: bool)
            
        Note:
            If event already exists, returns existing event with was_inserted=False
        """
        stmt = insert(CallEvent).values(
            call_id=call_id,
            event_type=event_type,
            sequence_number=sequence_number,
            event_timestamp=event_timestamp,
            payload=payload,
            previous_state=previous_state,
            new_state=new_state,
            processing_duration_ms=processing_duration_ms,
            received_at=datetime.utcnow(),
            processed_at=datetime.utcnow(),
        )

        # PostgreSQL-specific: ON CONFLICT DO NOTHING
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["call_id", "sequence_number"]
        )

        try:
            result = await self.db.execute(stmt)
            await self.db.flush()

            # Check if row was inserted
            was_inserted = result.rowcount > 0

            if was_inserted:
                logger.debug(
                    f"Event inserted: call_id={call_id}, seq={sequence_number}, "
                    f"type={event_type}"
                )
            else:
                logger.warning(
                    f"Duplicate event ignored: call_id={call_id}, "
                    f"seq={sequence_number} (idempotency)"
                )

            # Fetch the event (either newly inserted or existing)
            fetch_stmt = select(CallEvent).where(
                CallEvent.call_id == call_id,
                CallEvent.sequence_number == sequence_number,
            )
            fetch_result = await self.db.execute(fetch_stmt)
            event = fetch_result.scalar_one()

            return event, was_inserted

        except IntegrityError as e:
            # Shouldn't happen with ON CONFLICT, but handle gracefully
            logger.error(f"Integrity error inserting event: {e}")
            raise DuplicateEventError(call_id, sequence_number)

    async def get_latest_sequence(self, call_id: str) -> Optional[int]:
        """
        Get the latest sequence number for a call.
        
        Used for gap detection.
        
        Args:
            call_id: Call identifier
            
        Returns:
            Latest sequence number or None if no events
        """
        stmt = (
            select(CallEvent.sequence_number)
            .where(CallEvent.call_id == call_id)
            .order_by(CallEvent.sequence_number.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_event_count(self, call_id: str) -> int:
        """
        Get total event count for a call.
        
        Args:
            call_id: Call identifier
            
        Returns:
            Number of events
        """
        stmt = select(CallEvent).where(CallEvent.call_id == call_id)
        result = await self.db.execute(stmt)
        return len(result.scalars().all())
