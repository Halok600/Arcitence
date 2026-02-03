"""Periodic worker for retrying failed AI processing tasks."""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.models.call import Call
from app.services.ai_processing_service import AIProcessingService

logger = logging.getLogger(__name__)


class PeriodicRetryWorker:
    """
    Background worker that periodically retries failed AI processing.
    
    Design:
    - Runs in separate asyncio task (started on app lifespan)
    - Polls database every 5 minutes
    - Picks up failed tasks that haven't been retried recently
    - Respects max retry count (5 attempts)
    - Gracefully handles shutdown
    """

    def __init__(
        self,
        interval_seconds: int = 300,  # 5 minutes
        max_retries: int = 5,
        retry_delay_minutes: int = 10,
    ):
        self.interval_seconds = interval_seconds
        self.max_retries = max_retries
        self.retry_delay_minutes = retry_delay_minutes
        self.running = False
        self._task: asyncio.Task = None

    async def find_retryable_calls(self) -> list[Call]:
        """
        Find calls eligible for AI retry.
        
        Criteria:
        - ai_status = 'FAILED'
        - retry_count < max_retries
        - Last retry was > retry_delay_minutes ago (or never retried)
        - Call state is FAILED (not archived or in progress)
        
        Returns:
            List of Call objects to retry
        """
        async with AsyncSessionLocal() as db:
            retry_cutoff = datetime.utcnow() - timedelta(
                minutes=self.retry_delay_minutes
            )

            stmt = (
                select(Call)
                .where(
                    Call.ai_status == "FAILED",
                    Call.ai_retry_count < self.max_retries,
                    Call.state == "FAILED",  # Don't retry archived/active calls
                )
                .where(
                    # Never retried OR last retry was long ago
                    (Call.ai_last_retry_at.is_(None))
                    | (Call.ai_last_retry_at < retry_cutoff)
                )
                .order_by(Call.ai_last_retry_at.asc().nullsfirst())
                .limit(100)  # Process max 100 per iteration
            )

            result = await db.execute(stmt)
            calls = result.scalars().all()

            logger.info(f"Found {len(calls)} calls eligible for AI retry")
            return list(calls)

    async def cleanup_stuck_processing(self) -> int:
        """
        Clean up calls stuck in PROCESSING_AI state.
        
        If a process crashes while processing AI, the call will be stuck
        in PROCESSING_AI state. This function detects and resets them.
        
        Detection:
        - ai_status = 'PROCESSING'
        - updated_at > 5 minutes ago (assuming processing takes <5 min)
        
        Returns:
            Number of stuck calls reset
        """
        async with AsyncSessionLocal() as db:
            stuck_cutoff = datetime.utcnow() - timedelta(minutes=5)

            stmt = select(Call).where(
                Call.ai_status == "PROCESSING", Call.updated_at < stuck_cutoff
            )

            result = await db.execute(stmt)
            stuck_calls = result.scalars().all()

            if not stuck_calls:
                return 0

            logger.warning(f"Found {len(stuck_calls)} stuck AI processing tasks")

            # Reset them to FAILED for retry
            for call in stuck_calls:
                service = AIProcessingService(db)
                await service.update_ai_status(
                    call.call_id,
                    "FAILED",
                    error_message="Processing stuck, reset by cleanup worker",
                )

            await db.commit()

            logger.info(f"Reset {len(stuck_calls)} stuck calls")
            return len(stuck_calls)

    async def retry_call(self, call_id: str) -> bool:
        """
        Retry AI processing for a single call.
        
        Args:
            call_id: Call to retry
            
        Returns:
            True if successful, False if failed
        """
        try:
            async with AsyncSessionLocal() as db:
                service = AIProcessingService(db)
                success = await service.process_call(call_id)

                if success:
                    logger.info(f"Retry succeeded: {call_id}")
                else:
                    logger.warning(f"Retry failed: {call_id}")

                return success

        except Exception as e:
            logger.error(f"Error retrying {call_id}: {e}", exc_info=True)
            return False

    async def process_retry_batch(self, calls: list[Call]) -> dict:
        """
        Process a batch of calls concurrently.
        
        Args:
            calls: List of calls to retry
            
        Returns:
            dict with 'success', 'failed', 'total' counts
        """
        if not calls:
            return {"success": 0, "failed": 0, "total": 0}

        logger.info(f"Processing retry batch of {len(calls)} calls")

        # Process concurrently with limited parallelism
        semaphore = asyncio.Semaphore(10)  # Max 10 concurrent AI calls

        async def retry_with_semaphore(call_id: str) -> bool:
            async with semaphore:
                return await self.retry_call(call_id)

        # Create tasks for all calls
        tasks = [retry_with_semaphore(call.call_id) for call in calls]

        # Wait for all to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successes and failures
        success_count = sum(1 for r in results if r is True)
        failed_count = len(results) - success_count

        logger.info(
            f"Retry batch complete: {success_count} succeeded, "
            f"{failed_count} failed out of {len(calls)}"
        )

        return {"success": success_count, "failed": failed_count, "total": len(calls)}

    async def run_iteration(self) -> None:
        """Run one iteration of the retry worker."""
        try:
            logger.info("Retry worker iteration starting")

            # Step 1: Clean up stuck processing
            stuck_count = await self.cleanup_stuck_processing()

            # Step 2: Find retryable calls
            calls = await self.find_retryable_calls()

            # Step 3: Process batch
            if calls:
                stats = await self.process_retry_batch(calls)
                logger.info(f"Retry stats: {stats}")
            else:
                logger.info("No calls to retry")

            logger.info("Retry worker iteration complete")

        except Exception as e:
            logger.error(f"Error in retry worker iteration: {e}", exc_info=True)

    async def run(self) -> None:
        """
        Main worker loop.
        
        Runs continuously until stopped, executing iterations at fixed intervals.
        """
        self.running = True
        logger.info(
            f"Retry worker started (interval: {self.interval_seconds}s, "
            f"max_retries: {self.max_retries})"
        )

        while self.running:
            try:
                await self.run_iteration()

                # Wait for next interval
                await asyncio.sleep(self.interval_seconds)

            except asyncio.CancelledError:
                logger.info("Retry worker cancelled")
                break

            except Exception as e:
                logger.error(f"Fatal error in retry worker: {e}", exc_info=True)
                # Wait before retrying to avoid tight loop
                await asyncio.sleep(60)

        logger.info("Retry worker stopped")

    def start(self) -> asyncio.Task:
        """
        Start the worker in background.
        
        Returns:
            asyncio.Task for the worker
        """
        if self._task is not None and not self._task.done():
            logger.warning("Retry worker already running")
            return self._task

        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Stop the worker gracefully."""
        if self._task is None or self._task.done():
            logger.warning("Retry worker not running")
            return

        logger.info("Stopping retry worker...")
        self.running = False

        # Cancel the task
        self._task.cancel()

        try:
            await self._task
        except asyncio.CancelledError:
            pass

        logger.info("Retry worker stopped")


# Global worker instance
_retry_worker: PeriodicRetryWorker = None


def get_retry_worker() -> PeriodicRetryWorker:
    """Get global retry worker instance."""
    global _retry_worker
    if _retry_worker is None:
        _retry_worker = PeriodicRetryWorker(
            interval_seconds=300,  # 5 minutes
            max_retries=5,
            retry_delay_minutes=10,
        )
    return _retry_worker


async def start_retry_worker() -> asyncio.Task:
    """
    Start retry worker (called from app lifespan).
    
    Returns:
        asyncio.Task for the worker
    """
    worker = get_retry_worker()
    task = worker.start()
    logger.info("Retry worker started")
    return task


async def stop_retry_worker() -> None:
    """
    Stop retry worker (called from app lifespan).
    """
    worker = get_retry_worker()
    await worker.stop()
    logger.info("Retry worker stopped")
