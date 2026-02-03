"""Background AI processing service with robust retry logic."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    retry_if_exception_type,
    before_sleep_log,
    after_log,
)

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.call import AIResult, Call
from app.utils.state_machine import CallStateMachine, InvalidStateTransitionError
from app.utils.retry_strategy import (
    is_retryable_http_error,
    NonRetryableError,
    retry_stats,
)
from app.core.websocket_manager import websocket_manager

logger = logging.getLogger(__name__)


class AIServiceError(Exception):
    """Base exception for AI service errors."""

    pass


class AIServiceTimeoutError(AIServiceError):
    """AI service timeout."""

    pass


class AIServiceClientError(AIServiceError):
    """AI service client error (4xx) - non-retryable."""

    pass


class AIService:
    """
    Client for external AI service with retry logic.
    
    Design:
    - Uses httpx for async HTTP
    - Exponential backoff for transient errors (5xx)
    - No retry for client errors (4xx)
    - Circuit breaker pattern (future enhancement)
    """

    def __init__(self):
        self.base_url = settings.ai_service_url
        self.timeout = settings.ai_service_timeout
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(
                max_keepalive_connections=20, max_connections=100
            ),
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_random_exponential(multiplier=1, min=1, max=60),
        retry=retry_if_exception_type(
            (AIServiceError, AIServiceTimeoutError, httpx.HTTPStatusError, httpx.TimeoutException)
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def transcribe(self, recording_url: str, call_id: str = "unknown") -> dict:
        """
        Transcribe call recording with exponential backoff + jitter.
        
        Retry strategy:
        - Max 3 attempts
        - Exponential backoff: 1s, 2s, 4s, 8s... (capped at 60s)
        - Full jitter to prevent retry storms
        - Only retries on 5xx errors and timeouts
        - No retry on 4xx client errors
        
        Args:
            recording_url: URL of the call recording
            call_id: Call identifier for logging
            
        Returns:
            dict with 'text', 'confidence', 'language'
            
        Raises:
            AIServiceTimeoutError: If service times out (retryable)
            AIServiceClientError: If request is invalid (NON-retryable)
            AIServiceError: If service fails with 5xx (retryable)
        """
        attempt = getattr(self.transcribe.retry, 'statistics', {}).get('attempt_number', 1)
        
        logger.info(
            f"AI transcription attempt {attempt} for call {call_id}",
            extra={
                "call_id": call_id,
                "attempt": attempt,
                "recording_url": recording_url[:100]
            }
        )
        
        try:
            response = await self.client.post(
                f"{self.base_url}/transcribe",
                json={"audio_url": recording_url, "call_id": call_id},
            )

            # Check for timeout status codes
            if response.status_code in (408, 504):
                logger.warning(
                    f"AI transcription timeout {response.status_code} for {call_id}"
                )
                raise AIServiceTimeoutError(f"Timeout: {response.status_code}")

            # Check for client errors (4xx) - NON-RETRYABLE
            if 400 <= response.status_code < 500:
                logger.error(
                    f"AI transcription client error {response.status_code} for {call_id}: "
                    f"{response.text[:200]}",
                    extra={
                        "call_id": call_id,
                        "status_code": response.status_code,
                        "response": response.text[:200]
                    }
                )
                # Mark as non-retryable and raise immediately
                retry_stats.record_failure(attempt, is_retryable=False)
                raise AIServiceClientError(
                    f"Client error {response.status_code}: {response.text}"
                )

            # Check for server errors (5xx) - RETRYABLE
            if response.status_code >= 500:
                logger.warning(
                    f"AI transcription server error {response.status_code} for {call_id} "
                    f"(will retry)",
                    extra={
                        "call_id": call_id,
                        "status_code": response.status_code,
                        "attempt": attempt
                    }
                )
                raise AIServiceError(
                    f"Server error {response.status_code}: {response.text}"
                )

            response.raise_for_status()
            result = response.json()
            
            # Success - record stats
            retry_stats.record_success(attempt)
            
            logger.info(
                f"AI transcription succeeded for {call_id} (attempt {attempt})",
                extra={
                    "call_id": call_id,
                    "attempt": attempt,
                    "transcript_length": len(result.get('transcript', ''))
                }
            )
            
            return result

        except httpx.TimeoutException as e:
            logger.warning(
                f"AI transcription network timeout for {call_id}: {e}",
                extra={"call_id": call_id, "attempt": attempt}
            )
            raise AIServiceTimeoutError(str(e))

        except httpx.HTTPError as e:
            logger.error(
                f"AI transcription HTTP error for {call_id}: {e}",
                extra={"call_id": call_id, "attempt": attempt}
            )
            raise AIServiceError(str(e))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_random_exponential(multiplier=1, min=1, max=60),
        retry=retry_if_exception_type(
            (AIServiceError, AIServiceTimeoutError, httpx.HTTPStatusError, httpx.TimeoutException)
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def analyze_sentiment(self, transcript: str, call_id: str = "unknown") -> dict:
        """
        Analyze sentiment of call transcript with exponential backoff + jitter.
        
        Retry strategy:
        - Max 3 attempts
        - Exponential backoff with jitter (1-60s)
        - Only retries on 5xx errors and timeouts
        - No retry on 4xx client errors
        
        Args:
            transcript: Transcribed text
            call_id: Call identifier for logging
            
        Returns:
            dict with 'sentiment', 'score', 'emotions'
            
        Raises:
            AIServiceTimeoutError: If service times out (retryable)
            AIServiceClientError: If request is invalid (NON-retryable)
            AIServiceError: If service fails with 5xx (retryable)
        """
        attempt = getattr(self.analyze_sentiment.retry, 'statistics', {}).get('attempt_number', 1)
        
        logger.info(
            f"AI sentiment analysis attempt {attempt} for call {call_id}",
            extra={
                "call_id": call_id,
                "attempt": attempt,
                "transcript_length": len(transcript)
            }
        )
        
        try:
            response = await self.client.post(
                f"{self.base_url}/analyze_sentiment",
                json={"text": transcript, "call_id": call_id},
            )

            # Check for timeout status codes
            if response.status_code in (408, 504):
                logger.warning(
                    f"AI sentiment timeout {response.status_code} for {call_id}"
                )
                raise AIServiceTimeoutError(f"Timeout: {response.status_code}")

            # Check for client errors (4xx) - NON-RETRYABLE
            if 400 <= response.status_code < 500:
                logger.error(
                    f"AI sentiment client error {response.status_code} for {call_id}: "
                    f"{response.text[:200]}",
                    extra={
                        "call_id": call_id,
                        "status_code": response.status_code,
                        "response": response.text[:200]
                    }
                )
                # Mark as non-retryable
                retry_stats.record_failure(attempt, is_retryable=False)
                raise AIServiceClientError(
                    f"Client error {response.status_code}: {response.text}"
                )

            # Check for server errors (5xx) - RETRYABLE
            if response.status_code >= 500:
                logger.warning(
                    f"AI sentiment server error {response.status_code} for {call_id} "
                    f"(will retry)",
                    extra={
                        "call_id": call_id,
                        "status_code": response.status_code,
                        "attempt": attempt
                    }
                )
                raise AIServiceError(
                    f"Server error {response.status_code}: {response.text}"
                )

            response.raise_for_status()
            result = response.json()
            
            # Success - record stats
            retry_stats.record_success(attempt)
            
            logger.info(
                f"AI sentiment analysis succeeded for {call_id} (attempt {attempt})",
                extra={
                    "call_id": call_id,
                    "attempt": attempt,
                    "sentiment": result.get('sentiment')
                }
            )
            
            return result

        except httpx.TimeoutException as e:
            logger.warning(
                f"AI sentiment network timeout for {call_id}: {e}",
                extra={"call_id": call_id, "attempt": attempt}
            )
            raise AIServiceTimeoutError(str(e))

        except httpx.HTTPError as e:
            logger.error(
                f"AI sentiment HTTP error for {call_id}: {e}",
                extra={"call_id": call_id, "attempt": attempt}
            )
            raise AIServiceError(str(e))

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


class AIProcessingService:
    """
    Orchestrates AI processing pipeline with state safety.
    
    Responsibilities:
    - Manage state transitions (COMPLETED → PROCESSING_AI → COMPLETED/FAILED)
    - Call AI services with retry
    - Store results in database
    - Handle failures gracefully
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.ai_service = AIService()

    async def safe_transition(
        self,
        call_id: str,
        from_state: str,
        to_state: str,
        max_retries: int = 3,
    ) -> Optional[Call]:
        """
        Safely transition call state with optimistic locking.
        
        Args:
            call_id: Call identifier
            from_state: Expected current state
            to_state: Desired new state
            max_retries: Max retry attempts on version conflict
            
        Returns:
            Updated Call or None if transition failed/aborted
        """
        for attempt in range(max_retries):
            # Read current call
            stmt = select(Call).where(Call.call_id == call_id)
            result = await self.db.execute(stmt)
            call = result.scalar_one_or_none()

            if call is None:
                logger.error(f"Call not found: {call_id}")
                return None

            # Idempotent: already in target state
            if call.state == to_state:
                logger.info(
                    f"Call {call_id} already in state {to_state}, skipping"
                )
                return call

            # Check if in expected source state
            if call.state != from_state:
                logger.warning(
                    f"Call {call_id} in unexpected state: "
                    f"expected {from_state}, got {call.state}, aborting"
                )
                return None  # Abort - state changed

            # Validate state machine transition
            try:
                CallStateMachine.validate_transition(from_state, to_state, call_id)
            except InvalidStateTransitionError as e:
                logger.error(f"Invalid transition: {e}")
                return None

            # Attempt transition with optimistic lock + state check
            update_stmt = (
                update(Call)
                .where(
                    Call.call_id == call_id,
                    Call.version == call.version,
                    Call.state == from_state,  # Critical: double-check state
                )
                .values(
                    state=to_state,
                    version=call.version + 1,
                    updated_at=datetime.utcnow(),
                )
                .returning(Call)
            )

            update_result = await self.db.execute(update_stmt)
            updated_call = update_result.scalar_one_or_none()

            if updated_call is not None:
                logger.info(f"Transition: {call_id} {from_state} → {to_state}")
                return updated_call

            # Version conflict - retry
            logger.warning(
                f"Optimistic lock conflict for {call_id} (attempt {attempt + 1})"
            )

            if attempt < max_retries - 1:
                await asyncio.sleep(0.1 * (2**attempt))
                continue

        logger.error(f"Failed to transition {call_id} after {max_retries} attempts")
        return None

    async def update_ai_status(
        self,
        call_id: str,
        ai_status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Update AI processing status separately from call state.
        
        Args:
            call_id: Call identifier
            ai_status: PENDING, PROCESSING, COMPLETED, or FAILED
            error_message: Error description if failed
        """
        stmt = select(Call).where(Call.call_id == call_id)
        result = await self.db.execute(stmt)
        call = result.scalar_one_or_none()

        if call is None:
            return

        update_stmt = (
            update(Call)
            .where(Call.call_id == call_id, Call.version == call.version)
            .values(
                ai_status=ai_status,
                ai_retry_count=Call.ai_retry_count + 1,
                ai_last_retry_at=datetime.utcnow(),
                version=call.version + 1,
                updated_at=datetime.utcnow(),
            )
        )

        await self.db.execute(update_stmt)
        await self.db.commit()

        logger.info(f"AI status updated: {call_id} → {ai_status}")

    async def store_ai_results(
        self,
        call_id: str,
        transcription: dict,
        sentiment: dict,
        processing_duration_ms: int,
    ) -> None:
        """
        Store AI processing results in ai_results table.
        
        Args:
            call_id: Call identifier
            transcription: Transcription result
            sentiment: Sentiment analysis result
            processing_duration_ms: Total processing time
        """
        # Store transcription result
        transcription_result = AIResult(
            call_id=call_id,
            analysis_type="TRANSCRIPTION",
            result_data=transcription,
            confidence_score=transcription.get("confidence"),
            processing_started_at=datetime.utcnow()
            - timedelta(milliseconds=processing_duration_ms),
            processing_completed_at=datetime.utcnow(),
            ai_service_duration_ms=processing_duration_ms,
        )
        self.db.add(transcription_result)

        # Store sentiment result
        sentiment_result = AIResult(
            call_id=call_id,
            analysis_type="SENTIMENT",
            result_data=sentiment,
            confidence_score=sentiment.get("score"),
            processing_started_at=datetime.utcnow()
            - timedelta(milliseconds=processing_duration_ms),
            processing_completed_at=datetime.utcnow(),
            ai_service_duration_ms=processing_duration_ms,
        )
        self.db.add(sentiment_result)

        await self.db.flush()
        logger.info(f"AI results stored: {call_id}")

    async def process_call(self, call_id: str) -> bool:
        """
        Complete AI processing pipeline for a call.
        
        Pipeline:
        1. Transition COMPLETED → PROCESSING_AI
        2. Fetch call details
        3. Call transcription service
        4. Call sentiment analysis service
        5. Store results
        6. Transition PROCESSING_AI → COMPLETED
        
        Args:
            call_id: Call to process
            
        Returns:
            True if successful, False if failed
        """
        start_time = datetime.utcnow()

        try:
            # ═══════════════════════════════════════════════════════════
            # STEP 1: Transition to PROCESSING_AI
            # ═══════════════════════════════════════════════════════════

            call = await self.safe_transition(
                call_id, from_state="COMPLETED", to_state="PROCESSING_AI"
            )

            if call is None:
                logger.info(f"Skipping AI processing for {call_id} (state changed)")
                return False

            # Update ai_status
            await self.update_ai_status(call_id, "PROCESSING")
            await self.db.commit()

            # ═══════════════════════════════════════════════════════════
            # STEP 2: Fetch call details (outside transaction)
            # ═══════════════════════════════════════════════════════════

            stmt = select(Call).where(Call.call_id == call_id)
            result = await self.db.execute(stmt)
            call = result.scalar_one()

            if not call.recording_url:
                raise ValueError(f"No recording URL for call {call_id}")

            # ═══════════════════════════════════════════════════════════
            # STEP 3: Call AI services (with retry via tenacity)
            # ═══════════════════════════════════════════════════════════

            logger.info(f"Starting AI processing: {call_id}")

            # Transcription (with retry via tenacity)
            transcription = await self.ai_service.transcribe(
                call.recording_url, call_id=call_id
            )
            logger.info(f"Transcription completed: {call_id}")

            # Sentiment analysis (with retry via tenacity)
            transcript_text = transcription.get("transcript", "")
            sentiment = await self.ai_service.analyze_sentiment(
                transcript_text, call_id=call_id
            )
            logger.info(f"Sentiment analysis completed: {call_id}")

            # ═══════════════════════════════════════════════════════════
            # STEP 4: Store results
            # ═══════════════════════════════════════════════════════════

            processing_duration_ms = int(
                (datetime.utcnow() - start_time).total_seconds() * 1000
            )

            await self.store_ai_results(
                call_id, transcription, sentiment, processing_duration_ms
            )

            # ═══════════════════════════════════════════════════════════
            # STEP 5: Transition back to COMPLETED
            # ═══════════════════════════════════════════════════════════

            call = await self.safe_transition(
                call_id, from_state="PROCESSING_AI", to_state="COMPLETED"
            )

            if call is None:
                logger.warning(f"Failed to transition {call_id} back to COMPLETED")
                # Results stored, but state stuck - manual intervention needed
                return False

            # Update ai_status
            await self.update_ai_status(call_id, "COMPLETED")
            await self.db.commit()

            logger.info(
                f"AI processing completed: {call_id} "
                f"(duration: {processing_duration_ms}ms)"
            )
            
            # Broadcast AI completion to supervisors (non-blocking)
            asyncio.create_task(
                websocket_manager.broadcast(
                    event_type="AI_COMPLETED",
                    call_id=call_id,
                    data={
                        "processing_duration_ms": processing_duration_ms,
                        "transcript": transcription.get("transcript", "")[:100],
                        "sentiment": sentiment.get("sentiment"),
                        "sentiment_score": sentiment.get("score"),
                    },
                    agent_id=call.agent_id if hasattr(call, 'agent_id') else None
                )
            )

            return True

        except AIServiceClientError as e:
            # Client error (4xx) - NON-RETRYABLE
            # These are permanent failures (bad request, auth failure, etc.)
            logger.error(
                f"AI client error for {call_id} - PERMANENT FAILURE: {e}",
                extra={
                    "call_id": call_id,
                    "error_type": "client_error",
                    "retryable": False
                }
            )

            await self.safe_transition(
                call_id, from_state="PROCESSING_AI", to_state="FAILED"
            )
            await self.update_ai_status(
                call_id, 
                "FAILED", 
                error_message=f"NON-RETRYABLE: {str(e)}"
            )
            await self.db.commit()
            
            # Record non-retryable failure
            retry_stats.record_failure(attempts=3, is_retryable=False)
            
            # Broadcast AI failure to supervisors (non-blocking)
            asyncio.create_task(
                websocket_manager.broadcast(
                    event_type="AI_FAILED",
                    call_id=call_id,
                    data={
                        "error_type": "client_error",
                        "error_message": str(e)[:200],
                        "retryable": False,
                    }
                )
            )

            return False

        except (AIServiceTimeoutError, AIServiceError) as e:
            # Transient error (5xx, timeout) - RETRYABLE
            # Will be retried by periodic worker
            logger.warning(
                f"AI service transient error for {call_id} - WILL RETRY: {e}",
                extra={
                    "call_id": call_id,
                    "error_type": "transient_error",
                    "retryable": True,
                    "retry_stats": retry_stats.get_stats()
                }
            )

            await self.safe_transition(
                call_id, from_state="PROCESSING_AI", to_state="FAILED"
            )
            await self.update_ai_status(
                call_id, 
                "FAILED", 
                error_message=f"RETRYABLE: {str(e)}"
            )
            await self.db.commit()
            
            # Record retryable failure (will be retried by worker)
            retry_stats.record_failure(attempts=3, is_retryable=True)
            
            # Broadcast AI failure to supervisors (non-blocking)
            asyncio.create_task(
                websocket_manager.broadcast(
                    event_type="AI_FAILED",
                    call_id=call_id,
                    data={
                        "error_type": "transient_error",
                        "error_message": str(e)[:200],
                        "retryable": True,
                        "will_retry": True,
                    }
                )
            )

            return False

        except Exception as e:
            # Unexpected error
            logger.error(f"Unexpected error processing {call_id}: {e}", exc_info=True)

            try:
                await self.safe_transition(
                    call_id, from_state="PROCESSING_AI", to_state="FAILED"
                )
                await self.update_ai_status(
                    call_id, "FAILED", error_message=f"Unexpected: {str(e)}"
                )
                await self.db.commit()
            except Exception as rollback_error:
                logger.error(f"Failed to rollback state: {rollback_error}")

            return False


async def trigger_ai_processing_background(call_id: str):
    """
    Background task entry point for AI processing.
    
    Triggered by FastAPI BackgroundTasks when call becomes COMPLETED.
    
    Design:
    - Creates own database session (independent of request)
    - Handles all errors gracefully (never raises to caller)
    - Logs all outcomes for observability
    
    Args:
        call_id: Call to process
    """
    try:
        async with AsyncSessionLocal() as db:
            service = AIProcessingService(db)
            success = await service.process_call(call_id)

            if success:
                logger.info(f"Background AI processing succeeded: {call_id}")
            else:
                logger.warning(f"Background AI processing failed: {call_id}")

    except Exception as e:
        logger.error(
            f"Fatal error in background AI processing for {call_id}: {e}",
            exc_info=True,
        )
