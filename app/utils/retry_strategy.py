"""
Robust Retry Strategy with Exponential Backoff

This module implements a production-grade retry mechanism that:
1. Handles transient 503 errors from external services
2. Uses exponential backoff with jitter to avoid retry storms
3. Provides async-safe implementation
4. Enforces max retry limits
5. Includes comprehensive logging

Algorithm:
    wait_time = min(max_wait, base_delay * (2 ^ attempt_number)) + jitter
    
    Where:
    - base_delay: Initial delay (e.g., 1 second)
    - attempt_number: 0, 1, 2, ... (0-indexed)
    - max_wait: Cap to prevent excessive delays (e.g., 60 seconds)
    - jitter: Random value to prevent thundering herd

Example progression (base=1s, max=60s):
    Attempt 0: wait 1s + jitter(0-1s)     = 1-2s
    Attempt 1: wait 2s + jitter(0-2s)     = 2-4s
    Attempt 2: wait 4s + jitter(0-4s)     = 4-8s
    Attempt 3: wait 8s + jitter(0-8s)     = 8-16s
    Attempt 4: wait 16s + jitter(0-16s)   = 16-32s
    Attempt 5: wait 32s + jitter(0-32s)   = 32-64s (capped at 60s)
"""

import asyncio
import logging
import random
from functools import wraps
from typing import Callable, TypeVar, ParamSpec, Optional, Type, Tuple
from datetime import datetime

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    after_log,
    wait_random_exponential
)

logger = logging.getLogger(__name__)

P = ParamSpec('P')
T = TypeVar('T')


# Retry configuration constants
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MIN_WAIT = 1  # seconds
DEFAULT_MAX_WAIT = 60  # seconds
DEFAULT_JITTER_MULTIPLIER = 1.0


class RetryableError(Exception):
    """Base class for errors that should trigger a retry."""
    pass


class TransientServiceError(RetryableError):
    """HTTP 503 or similar transient errors."""
    pass


class TimeoutError(RetryableError):
    """Request timeout errors."""
    pass


class NonRetryableError(Exception):
    """Errors that should NOT trigger a retry (e.g., 4xx client errors)."""
    pass


def is_retryable_http_error(exc: Exception) -> bool:
    """
    Determine if an HTTP error should trigger a retry.
    
    Retryable errors (5xx):
    - 500 Internal Server Error
    - 502 Bad Gateway
    - 503 Service Unavailable
    - 504 Gateway Timeout
    
    Non-retryable errors (4xx):
    - 400 Bad Request
    - 401 Unauthorized
    - 403 Forbidden
    - 404 Not Found
    - 422 Unprocessable Entity
    
    Args:
        exc: Exception to check
        
    Returns:
        True if error should trigger retry, False otherwise
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        
        # 5xx server errors are retryable
        if 500 <= status_code < 600:
            return True
        
        # 429 Too Many Requests is retryable
        if status_code == 429:
            return True
        
        # 4xx client errors are NOT retryable
        if 400 <= status_code < 500:
            return False
    
    # Timeout errors are retryable
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return True
    
    # Connection errors are retryable
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    
    # Unknown errors default to non-retryable
    return False


def create_async_retry_decorator(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
    jitter_multiplier: float = DEFAULT_JITTER_MULTIPLIER,
    retry_on: Optional[Tuple[Type[Exception], ...]] = None
) -> Callable:
    """
    Create an async retry decorator with exponential backoff and jitter.
    
    This implements the "exponential backoff with full jitter" algorithm
    recommended by AWS: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
    
    Algorithm:
        base_wait = min(max_wait, min_wait * (2 ^ attempt))
        actual_wait = random.uniform(0, base_wait * jitter_multiplier)
    
    Why jitter prevents retry storms:
    - Without jitter: All clients retry at exactly 1s, 2s, 4s, 8s...
    - With jitter: Clients retry at random times within each window
    - Result: Load is distributed over time instead of synchronized spikes
    
    Args:
        max_attempts: Maximum number of retry attempts (default: 3)
        min_wait: Minimum wait time in seconds (default: 1)
        max_wait: Maximum wait time in seconds (default: 60)
        jitter_multiplier: Jitter range multiplier (default: 1.0)
        retry_on: Tuple of exception types to retry on (default: retryable errors)
    
    Returns:
        Decorator function for async methods
        
    Example:
        @create_async_retry_decorator(max_attempts=5, min_wait=2)
        async def call_external_api():
            async with httpx.AsyncClient() as client:
                return await client.get("https://api.example.com")
    """
    if retry_on is None:
        # Default: retry on retryable HTTP errors and transient exceptions
        retry_on = (
            TransientServiceError,
            TimeoutError,
            httpx.HTTPStatusError,
            httpx.TimeoutException,
            httpx.ConnectError,
            asyncio.TimeoutError
        )
    
    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            attempt = 0
            last_exception = None
            
            async for attempt_obj in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_random_exponential(
                    multiplier=jitter_multiplier,
                    min=min_wait,
                    max=max_wait
                ),
                retry=retry_if_exception_type(retry_on),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                after=after_log(logger, logging.DEBUG),
                reraise=True
            ):
                with attempt_obj:
                    attempt = attempt_obj.retry_state.attempt_number
                    
                    try:
                        # Log attempt
                        if attempt > 1:
                            logger.info(
                                f"Retry attempt {attempt}/{max_attempts} for {func.__name__}",
                                extra={
                                    "function": func.__name__,
                                    "attempt": attempt,
                                    "max_attempts": max_attempts
                                }
                            )
                        
                        # Execute function
                        result = await func(*args, **kwargs)
                        
                        # Log success after retry
                        if attempt > 1:
                            logger.info(
                                f"Success after {attempt} attempts for {func.__name__}",
                                extra={
                                    "function": func.__name__,
                                    "total_attempts": attempt
                                }
                            )
                        
                        return result
                        
                    except Exception as exc:
                        last_exception = exc
                        
                        # Check if error is retryable
                        if isinstance(exc, httpx.HTTPStatusError):
                            if not is_retryable_http_error(exc):
                                logger.error(
                                    f"Non-retryable HTTP error in {func.__name__}: "
                                    f"{exc.response.status_code}",
                                    extra={
                                        "function": func.__name__,
                                        "status_code": exc.response.status_code,
                                        "response": exc.response.text[:200]
                                    }
                                )
                                raise NonRetryableError(
                                    f"Non-retryable HTTP {exc.response.status_code}"
                                ) from exc
                        
                        # Log retryable error
                        logger.warning(
                            f"Retryable error in {func.__name__} (attempt {attempt}): {str(exc)[:100]}",
                            extra={
                                "function": func.__name__,
                                "attempt": attempt,
                                "max_attempts": max_attempts,
                                "error_type": type(exc).__name__,
                                "error_message": str(exc)[:200]
                            }
                        )
                        
                        # Re-raise to trigger retry
                        raise
            
            # Should never reach here due to reraise=True, but just in case
            raise last_exception
        
        return wrapper
    
    return decorator


# Pre-configured decorators for common scenarios

# Standard retry for AI service calls (3 attempts, 1-60s backoff)
async_retry_with_backoff = create_async_retry_decorator(
    max_attempts=3,
    min_wait=1,
    max_wait=60,
    jitter_multiplier=1.0
)

# Aggressive retry for critical operations (5 attempts, 2-120s backoff)
async_retry_aggressive = create_async_retry_decorator(
    max_attempts=5,
    min_wait=2,
    max_wait=120,
    jitter_multiplier=1.0
)

# Fast retry for low-latency operations (3 attempts, 0.5-10s backoff)
async_retry_fast = create_async_retry_decorator(
    max_attempts=3,
    min_wait=0.5,
    max_wait=10,
    jitter_multiplier=1.0
)


class RetryStats:
    """
    Track retry statistics for monitoring and alerting.
    
    Useful for:
    - Detecting degraded external services (high retry rate)
    - Capacity planning (average retry attempts)
    - Alerting on retry exhaustion (max_retries_exhausted)
    """
    
    def __init__(self):
        self.total_calls = 0
        self.successful_calls = 0
        self.failed_calls = 0
        self.total_retries = 0
        self.max_retries_exhausted = 0
        self.non_retryable_errors = 0
    
    def record_success(self, attempts: int):
        """Record a successful call (possibly after retries)."""
        self.total_calls += 1
        self.successful_calls += 1
        if attempts > 1:
            self.total_retries += (attempts - 1)
    
    def record_failure(self, attempts: int, is_retryable: bool):
        """Record a failed call after exhausting retries."""
        self.total_calls += 1
        self.failed_calls += 1
        if is_retryable:
            self.max_retries_exhausted += 1
            self.total_retries += (attempts - 1)
        else:
            self.non_retryable_errors += 1
    
    def get_stats(self) -> dict:
        """Get current statistics."""
        if self.total_calls == 0:
            return {
                "total_calls": 0,
                "success_rate": 0.0,
                "retry_rate": 0.0,
                "avg_retries_per_call": 0.0
            }
        
        return {
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "success_rate": self.successful_calls / self.total_calls,
            "retry_rate": self.total_retries / self.total_calls,
            "avg_retries_per_call": self.total_retries / self.total_calls,
            "max_retries_exhausted": self.max_retries_exhausted,
            "non_retryable_errors": self.non_retryable_errors
        }
    
    def reset(self):
        """Reset all statistics."""
        self.__init__()


# Global retry stats instance
retry_stats = RetryStats()


# Example usage
if __name__ == "__main__":
    import httpx
    
    @async_retry_with_backoff
    async def example_api_call():
        """Example API call with retry logic."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "http://localhost:8001/transcribe",
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()
    
    async def main():
        try:
            result = await example_api_call()
            print(f"Success: {result}")
        except Exception as e:
            print(f"Failed after retries: {e}")
    
    asyncio.run(main())
