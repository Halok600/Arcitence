# PBX Call Processing Microservice

Microservice for handling concurrent call streams from a PBX system with AI processing (transcription & sentiment analysis) and real-time WebSocket updates. Designed to deal with unreliable external services.

## Approach

The main challenges were handling high concurrency, dealing with flaky external AI services, and preventing race conditions. The solution uses async/await throughout for non-blocking I/O, separates ingestion from processing, and implements optimistic locking to handle concurrent updates safely.

Key decision: Return 202 Accepted immediately, then process AI stuff in the background. This keeps response times under 50ms even when the AI service is being slow.

## Technical Decisions

### Non-Blocking Ingestion

The API returns 202 Accepted immediately after writing to the database. AI processing happens in the background so it doesn't block the response. Using asyncpg for the PostgreSQL driver to keep everything async.

Connection pool is set to 20 with 10 overflow - seems to handle the load fine in testing.

### Handling Concurrent Updates

Using optimistic locking with a version field. When two requests try to update the same call simultaneously, one will see the version changed and retry. Max 3 retry attempts before giving up.

This approach avoids holding database locks and performs better than pessimistic locking for this use case.

### State Machine

Calls go through: `IN_PROGRESS → COMPLETED → PROCESSING_AI → FAILED → ARCHIVED`

The state machine validator prevents invalid transitions (e.g., can't go from ARCHIVED back to IN_PROGRESS). Both application-level and database constraints enforce this.

### Retry Strategy

The AI service fails ~25% of the time, so we retry with exponential backoff: 1s, 2s, 4s, etc. up to 60s max. Using the tenacity library for this.

There's also a background worker that runs every 5 minutes to pick up any failed AI tasks. Max 5 total attempts before marking as permanently failed.

### WebSocket Updates

Supervisors can connect via WebSocket to get real-time updates on call events. There's a heartbeat every 30s to keep connections alive, and the manager handles filtering so supervisors only get updates for their calls.

### Packet Ordering

Packets can arrive out of order due to network issues. We log warnings when there are sequence gaps but don't block processing. The database has a unique constraint on (call_id, sequence_number) to handle duplicates.

## Tech Stack

- FastAPI + Uvicorn (async web framework)
- PostgreSQL with asyncpg driver
- SQLAlchemy 2.0 for ORM
- Pydantic v2 for validation
- tenacity for retry logic
- pytest + httpx for testing

## Project Structure

```
articence/
├── app/
│   ├── api/v1/
│   │   ├── call.py              # Main ingestion endpoint
│   │   └── websocket.py         # WebSocket connections
│   ├── core/
│   │   ├── config.py            # Environment configuration
│   │   ├── database.py          # Async DB connection
│   │   └── websocket_manager.py # WebSocket state management
│   ├── models/
│   │   └── call.py              # SQLAlchemy models (Call, CallEvent, AIResult)
│   ├── repositories/
│   │   └── call_repository.py   # Data access layer with optimistic locking
│   ├── schemas/
│   │   └── call.py              # Pydantic request/response schemas
│   ├── services/
│   │   ├── call_service.py      # Business logic orchestration
│   │   ├── ai_processing_service.py # AI API client with retry
│   │   └── retry_worker.py      # Background worker for failed tasks
│   └── utils/
│       ├── state_machine.py     # State transition validator
│       └── retry_strategy.py    # Exponential backoff implementation
├── tests/
│   ├── test_concurrent_processing.py # Race condition tests
│   └── mock_ai_service.py       # Flaky AI service simulator
├── alembic/                     # Database migrations
├── .env                         # Environment variables
├── pyproject.toml               # Dependencies
├── run.py                       # Application entry point
└── README.md                    # This file
```

## Setup

Requirements: Python 3.11+, PostgreSQL 15+, Poetry

### Installation

```bash
# Clone the repository
cd Articence

# Create virtual environment (if not exists)
python -m venv .venv

# Activate virtual environment
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Windows CMD:
.venv\Scripts\activate.bat
# Linux/Mac:
source .venv/bin/activate

# Install Poetry
pip install poetry

# Install dependencies
poetry install
```

### Database Setup

Using Docker (easier):

```bash
# Start PostgreSQL container
docker-compose up -d postgres

# Database will be available at:
# postgresql://articence_user:articence_password_2026@localhost:5432/articence_db
```

Or native PostgreSQL:

```bash
# Create database
createdb articence_db

# Update .env file with your credentials
DATABASE_URL=postgresql+asyncpg://postgres:YOUR_PASSWORD@localhost:5432/articence_db
```

### 3. Run Database Migrations

```bash
# Run Alembic migrations
alembic upgrade head
```

### Environment Config

The `.env` file should be set up already. Main settings:

```env
# Database
DATABASE_URL=postgresql+asyncpg://postgres:Halok@123@localhost:5432/articence_db

# AI Service (points to mock service)
AI_SERVICE_URL=http://localhost:8001

# Server
HOST=0.0.0.0
PORT=8000
```

### Start Services

Terminal 1 - Mock AI Service:

```bash
python tests/mock_ai_service.py

# Server starts at http://localhost:8001
# Simulates 25% failure rate with 1-3s latency
```

Terminal 2 - Main API:

```bash
python run.py

# Server starts at http://localhost:8000
# API docs at http://localhost:8000/docs
```

### Verify It Works

Run the demo script:

```bash
python demo_full_requirements.py
```

Should see all requirements passing.

### Run Tests

```bash
# Run all integration tests
pytest tests/test_concurrent_processing.py -v

# Expected: 4 passed
```

## API Usage

Ingest a call packet:

```bash
curl -X POST http://localhost:8000/v1/call/stream/call_123 \
  -H "Content-Type: application/json" \
  -d '{
    "sequence": 1,
    "data": "audio_metadata_packet_001",
    "timestamp": 1738411815.123
  }'
```

Response:
```json
{
  "call_id": "call_123",
  "sequence": 1,
  "status": "accepted",
  "received_at": "2026-02-03T12:30:15.123Z",
  "response_time_ms": 12.5
}
```

Get call state:

```bash
curl http://localhost:8000/v1/call/call_123
```

WebSocket connection:

```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/supervisor/super_123');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Event:', data.event_type, data);
};
```

API docs available at http://localhost:8000/docs

## Testing

The integration tests check concurrent request handling:

- Duplicate packets (same sequence) - only one inserted
- Adjacent sequences arriving simultaneously - both succeed
- 50 concurrent requests - no lost updates
- Concurrent state transitions - consistent final state

```bash
pytest tests/test_concurrent_processing.py -v
```

## Performance

Response times average ~20ms, well under the 50ms target. Tested with 50+ concurrent requests without issues. Database connection pool is 20+10 overflow.\n\n## Notes

- Packet sequence gaps are logged but don't block processing
- Optimistic locking handles concurrent updates (3 retry attempts)
- Background worker retries failed AI tasks every 5 minutes
- WebSocket connections use heartbeats to stay alive

Database queries for debugging:

```sql
-- Check call states
SELECT call_id, state, ai_status, version FROM calls;

-- Check events
SELECT call_id, sequence_number, created_at FROM call_events ORDER BY created_at;

-- Check AI results
SELECT call_id, status, transcript FROM ai_results;
```

### 3. Run Database Migrations
```bash
# Requires PostgreSQL 14+
alembic upgrade head
```

### 4. Start Server
```bash
python -m app.main
# Or with uvicorn:
uvicorn app.main:app --reload
```

### 5. Test Endpoint
```bash
curl -X POST http://localhost:8000/v1/call/stream/call_123 \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "ANSWERED",
    "sequence_number": 1,
    "event_timestamp": "2026-02-01T10:30:00Z",
    "new_state": "IN_PROGRESS",
    "payload": {
      "caller_id": "+1234567890",
      "agent_id": "agent_001"
    }
  }'
```

## API Documentation

Interactive docs available at:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## Key Design Decisions

### 1. Optimistic Locking
**Why**: Prevents lost updates in concurrent scenarios
**How**: Version field increments on each update
**Result**: Safe concurrent packet processing

### 2. Sequence Gap Handling
**Why**: Networks can reorder/drop packets
**How**: Compare received sequence to latest stored
**Result**: Log warnings, continue processing (don't block)

### 3. Background AI Processing
**Why**: AI calls take 5-60 seconds
**How**: FastAPI BackgroundTasks
**Result**: <50ms response maintained

### 4. State Machine Validation
**Why**: Prevent invalid transitions (e.g., ARCHIVED → IN_PROGRESS)
**How**: Explicit VALID_TRANSITIONS map
**Result**: Data integrity enforced

## Database Schema

### calls table
- Primary entity with state machine
- Optimistic locking via `version`
- Partial indexes for performance

### call_events table
- Event sourcing for audit trail
- Unique constraint: (call_id, sequence_number)
- Used for gap detection

### ai_results table
- Separate from calls for scalability
- Multiple results per call supported
- Independent retry tracking

## Monitoring & Observability

**Metrics tracked:**
- Response time per request
- Sequence gaps detected
- Optimistic lock conflicts
- AI processing duration

**Logs:**
- Structured JSON logging
- Gap warnings (non-blocking)
- State transition audits
- Error stack traces

## Testing

```bash
# Unit tests
pytest tests/unit/

# Integration tests
pytest tests/integration/

# Load tests
locust -f tests/load/test_streaming.py
```

## Production Checklist

- [ ] Configure DATABASE_URL for production
- [ ] Set CORS allow_origins to specific domains
- [ ] Enable SSL/TLS (uvicorn --ssl-keyfile --ssl-certfile)
- [ ] Configure connection pool size based on load
- [ ] Set up monitoring (Prometheus/Grafana)
- [ ] Configure log aggregation (ELK/Datadog)
- [ ] Implement circuit breaker for AI service
- [ ] Set up database backups
- [ ] Configure horizontal scaling (K8s)
- [ ] Implement WebSocket manager for dashboard

## Next Steps

1. **AI Service Integration**
   - Implement retry logic with tenacity
   - Add circuit breaker pattern
   - Store results in ai_results table

2. **WebSocket Dashboard**
   - Connection manager
   - Broadcast on state changes
   - Heartbeat mechanism

3. **Metrics & Alerting**
   - Prometheus metrics endpoint
   - Alert on high gap frequency
   - SLA monitoring

## License
Proprietary - All Rights Reserved
