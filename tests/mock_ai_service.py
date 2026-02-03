"""
Mock External AI Transcription Service

This service simulates a flaky external AI API with:
- 25% failure rate (HTTP 503)
- Random latency (1-3 seconds)
- Realistic response payloads

Usage:
    python tests/mock_ai_service.py
    
    # Or with custom settings
    FAILURE_RATE=0.4 LATENCY_MIN=2 LATENCY_MAX=5 python tests/mock_ai_service.py

Testing with curl:
    # Transcription
    curl -X POST http://localhost:8001/transcribe \
      -H "Content-Type: application/json" \
      -d '{"audio_url": "s3://bucket/call_123.wav", "call_id": "call_123"}'
    
    # Sentiment Analysis
    curl -X POST http://localhost:8001/analyze_sentiment \
      -H "Content-Type: application/json" \
      -d '{"text": "Hello, this is a test call", "call_id": "call_123"}'
"""

import asyncio
import random
import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
import uvicorn


# Configuration (can be overridden via environment variables)
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0.25"))  # 25% default
LATENCY_MIN = float(os.getenv("LATENCY_MIN", "1.0"))     # 1 second min
LATENCY_MAX = float(os.getenv("LATENCY_MAX", "3.0"))     # 3 seconds max
PORT = int(os.getenv("MOCK_AI_PORT", "8001"))


# Request/Response Models
class TranscribeRequest(BaseModel):
    audio_url: str = Field(..., description="S3 URL or path to audio file")
    call_id: str = Field(..., description="Unique call identifier")
    language: Optional[str] = Field(default="en-US", description="Language code")


class TranscribeResponse(BaseModel):
    call_id: str
    transcript: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    duration_seconds: float
    word_count: int
    processing_time_ms: int


class SentimentRequest(BaseModel):
    text: str = Field(..., description="Text to analyze")
    call_id: str = Field(..., description="Unique call identifier")


class SentimentResponse(BaseModel):
    call_id: str
    sentiment: str = Field(..., description="positive, negative, or neutral")
    score: float = Field(..., ge=-1.0, le=1.0, description="Sentiment score")
    confidence: float = Field(..., ge=0.0, le=1.0)
    entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


# Mock AI Service
app = FastAPI(
    title="Mock AI Transcription Service",
    description="Simulates flaky external AI API for testing",
    version="1.0.0"
)


# Metrics tracking (for observability)
metrics = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "total_latency_seconds": 0.0
}


async def inject_chaos():
    """
    Inject randomness into the service behavior.
    
    This function implements the core chaos engineering logic:
    1. Random latency between LATENCY_MIN and LATENCY_MAX
    2. Random failures with probability FAILURE_RATE
    
    Raises:
        HTTPException: 503 Service Unavailable if random failure occurs
    """
    # Inject random latency (1-3 seconds by default)
    latency = random.uniform(LATENCY_MIN, LATENCY_MAX)
    await asyncio.sleep(latency)
    
    # Inject random failures (25% by default)
    if random.random() < FAILURE_RATE:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Temporarily Unavailable",
                "message": "Downstream AI service is experiencing high load",
                "retry_after": 10,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
    
    return latency


def generate_mock_transcript(audio_url: str) -> str:
    """Generate realistic-looking transcript based on audio URL."""
    templates = [
        "Hello, thank you for calling. How can I help you today?",
        "Hi there! I'm calling about my recent order. Can you look that up for me?",
        "Good morning. I'd like to inquire about your product pricing.",
        "Yes, I need to speak with someone in technical support please.",
        "Thank you so much for your help today. Have a great day!",
        "I'm sorry, I didn't quite catch that. Could you repeat?",
        "Let me transfer you to the right department. Please hold.",
        "Can I get your account number to pull up your information?"
    ]
    
    # Use hash of audio_url to deterministically select template
    seed = sum(ord(c) for c in audio_url)
    random.seed(seed)
    
    # Generate 2-5 sentences
    num_sentences = random.randint(2, 5)
    transcript = " ".join(random.choices(templates, k=num_sentences))
    
    # Reset random seed for chaos injection
    random.seed()
    
    return transcript


def analyze_mock_sentiment(text: str) -> tuple[str, float]:
    """Analyze sentiment based on keyword heuristics."""
    positive_words = {"thank", "thanks", "great", "excellent", "love", "perfect", "wonderful"}
    negative_words = {"problem", "issue", "terrible", "bad", "worst", "hate", "disappointed"}
    
    text_lower = text.lower()
    positive_count = sum(1 for word in positive_words if word in text_lower)
    negative_count = sum(1 for word in negative_words if word in text_lower)
    
    if positive_count > negative_count:
        sentiment = "positive"
        score = min(0.8, 0.5 + positive_count * 0.15)
    elif negative_count > positive_count:
        sentiment = "negative"
        score = max(-0.8, -0.5 - negative_count * 0.15)
    else:
        sentiment = "neutral"
        score = random.uniform(-0.1, 0.1)
    
    return sentiment, score


@app.middleware("http")
async def track_metrics(request: Request, call_next):
    """Track request metrics for observability."""
    metrics["total_requests"] += 1
    start_time = asyncio.get_event_loop().time()
    
    try:
        response = await call_next(request)
        
        if response.status_code < 500:
            metrics["successful_requests"] += 1
        else:
            metrics["failed_requests"] += 1
        
        return response
    except Exception as e:
        metrics["failed_requests"] += 1
        raise
    finally:
        elapsed = asyncio.get_event_loop().time() - start_time
        metrics["total_latency_seconds"] += elapsed


@app.get("/")
async def root():
    """Health check and service info."""
    avg_latency = (
        metrics["total_latency_seconds"] / metrics["total_requests"]
        if metrics["total_requests"] > 0
        else 0.0
    )
    
    return {
        "service": "Mock AI Transcription Service",
        "status": "operational",
        "config": {
            "failure_rate": FAILURE_RATE,
            "latency_range": f"{LATENCY_MIN}-{LATENCY_MAX}s"
        },
        "metrics": {
            **metrics,
            "success_rate": (
                metrics["successful_requests"] / metrics["total_requests"]
                if metrics["total_requests"] > 0
                else 0.0
            ),
            "average_latency_seconds": round(avg_latency, 3)
        }
    }


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(request: TranscribeRequest):
    """
    Transcribe audio to text.
    
    Simulates AI transcription with:
    - Random latency (1-3s)
    - 25% failure rate (503)
    - Realistic response payloads
    
    Args:
        request: TranscribeRequest with audio_url and call_id
        
    Returns:
        TranscribeResponse with transcript and metadata
        
    Raises:
        HTTPException: 503 if random failure occurs
    """
    # Inject chaos (latency + random failures)
    start_time = asyncio.get_event_loop().time()
    latency = await inject_chaos()
    
    # Generate mock transcript
    transcript = generate_mock_transcript(request.audio_url)
    word_count = len(transcript.split())
    
    # Calculate processing time
    processing_time_ms = int(latency * 1000)
    
    return TranscribeResponse(
        call_id=request.call_id,
        transcript=transcript,
        confidence=random.uniform(0.85, 0.98),
        duration_seconds=random.uniform(15.0, 180.0),
        word_count=word_count,
        processing_time_ms=processing_time_ms
    )


@app.post("/analyze_sentiment", response_model=SentimentResponse)
async def analyze_sentiment(request: SentimentRequest):
    """
    Analyze sentiment of text.
    
    Simulates AI sentiment analysis with:
    - Random latency (1-3s)
    - 25% failure rate (503)
    - Keyword-based sentiment scoring
    
    Args:
        request: SentimentRequest with text and call_id
        
    Returns:
        SentimentResponse with sentiment, score, and metadata
        
    Raises:
        HTTPException: 503 if random failure occurs
    """
    # Inject chaos (latency + random failures)
    latency = await inject_chaos()
    
    # Analyze sentiment
    sentiment, score = analyze_mock_sentiment(request.text)
    
    # Extract mock entities and keywords
    words = request.text.lower().split()
    keywords = [w for w in words if len(w) > 5][:3]  # Top 3 long words
    
    entities = []
    if "order" in request.text.lower():
        entities.append("order")
    if "product" in request.text.lower():
        entities.append("product")
    if "support" in request.text.lower():
        entities.append("customer_support")
    
    return SentimentResponse(
        call_id=request.call_id,
        sentiment=sentiment,
        score=score,
        confidence=random.uniform(0.75, 0.95),
        entities=entities,
        keywords=keywords
    )


@app.get("/metrics")
async def get_metrics():
    """
    Get service metrics for monitoring.
    
    Useful for observing:
    - Request success/failure rates
    - Average latency
    - Total requests processed
    """
    avg_latency = (
        metrics["total_latency_seconds"] / metrics["total_requests"]
        if metrics["total_requests"] > 0
        else 0.0
    )
    
    success_rate = (
        metrics["successful_requests"] / metrics["total_requests"]
        if metrics["total_requests"] > 0
        else 0.0
    )
    
    return {
        "total_requests": metrics["total_requests"],
        "successful_requests": metrics["successful_requests"],
        "failed_requests": metrics["failed_requests"],
        "success_rate": round(success_rate, 3),
        "average_latency_seconds": round(avg_latency, 3),
        "config": {
            "failure_rate": FAILURE_RATE,
            "latency_min": LATENCY_MIN,
            "latency_max": LATENCY_MAX
        }
    }


@app.post("/reset_metrics")
async def reset_metrics():
    """Reset all metrics (useful for testing)."""
    metrics["total_requests"] = 0
    metrics["successful_requests"] = 0
    metrics["failed_requests"] = 0
    metrics["total_latency_seconds"] = 0.0
    
    return {"message": "Metrics reset successfully"}


if __name__ == "__main__":
    print("=" * 80)
    print("🤖 Mock AI Transcription Service")
    print("=" * 80)
    print(f"📊 Configuration:")
    print(f"   - Failure Rate: {FAILURE_RATE * 100}%")
    print(f"   - Latency Range: {LATENCY_MIN}-{LATENCY_MAX} seconds")
    print(f"   - Port: {PORT}")
    print()
    print(f"🔗 Endpoints:")
    print(f"   - POST http://localhost:{PORT}/transcribe")
    print(f"   - POST http://localhost:{PORT}/analyze_sentiment")
    print(f"   - GET  http://localhost:{PORT}/metrics")
    print()
    print(f"💡 Test with curl:")
    print(f'   curl -X POST http://localhost:{PORT}/transcribe \\')
    print(f'     -H "Content-Type: application/json" \\')
    print(f'     -d \'{{"audio_url": "s3://bucket/call.wav", "call_id": "test_123"}}\'')
    print()
    print("=" * 80)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )
