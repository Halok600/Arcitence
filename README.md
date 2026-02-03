# PBX Call Processing System

This is a microservice I built to handle call streams from a PBX system. When agents aren't available, calls get routed to an AI voice bot. The tricky part? The AI service is pretty unreliable (it fails about 25% of the time), and we're getting thousands of concurrent calls that need to be processed without blocking.

## Why I Built It This Way

The biggest headaches were:
- Handling tons of concurrent requests without things grinding to a halt
- Dealing with an AI service that likes to fail randomly
- Making sure concurrent updates don't overwrite each other

I decided to use async/await everywhere to keep things non-blocking. The key insight was to separate "receiving data" from "processing data". When a call packet comes in, I immediately return 202 Accepted (takes ~20ms), then handle the heavy AI stuff in the background. This way, even if the AI service is being slow or failing, it doesn't block new requests.

## How It Works

### Fast API Responses

The API sends back 202 Accepted right after writing to the database. No waiting around for AI processing. I'm using asyncpg because it's the fastest async PostgreSQL driver I could find.

Connection pool is 20 + 10 overflow. Seems to work well so far.

### Race Condition Handling

Instead of database locks (which suck for performance), I went with optimistic locking. Each record has a version number. If two requests try to update the same call at once, one will see the version changed and automatically retry. Gives up after 3 tries.

Works way better than holding locks.

### State Machine

Calls flow through states: `IN_PROGRESS → COMPLETED → PROCESSING_AI → FAILED → ARCHIVED`

There's validation to prevent nonsense like going from ARCHIVED back to IN_PROGRESS. Enforced both in code and at the database level.

### Retry Logic

Since the AI service fails a lot (~25% of requests), I added exponential backoff: starts at 1s, then 2s, 4s, etc. up to 60s max. Using the tenacity library which handles this stuff nicely.

There's also a background worker that checks for failed tasks every 5 minutes and retries them. After 5 total attempts, it gives up.

### Real-time Updates

Supervisors can connect via WebSocket to see what's happening with calls in real-time. I send a heartbeat every 30s to keep connections alive. The connection manager filters events so supervisors only see updates for calls they care about.

### Packet Order

Network packets can arrive out of order. When I detect gaps in the sequence, I log a warning but keep processing anyway. The database has a unique constraint to catch duplicates automatically.

## What I Used

- **FastAPI** - Because it's fast and has great async support
- **PostgreSQL + asyncpg** - Reliable database with the fastest async driver
- **SQLAlchemy 2.0** - ORM that finally supports async properly
- **Pydantic v2** - Type validation (catches bugs early)
- **tenacity** - Retry logic without reinventing the wheel
- **pytest + httpx** - Testing concurrent requests

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

## Getting Started

You'll need Python 3.11 or higher, PostgreSQL 15+, and Poetry installed.

### 1. Install Dependencies

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

### 2. Set Up the Database

If you have Docker (easier way):

```bash
docker-compose up -d postgres
```

Or if you prefer native PostgreSQL:

```bash
createdb articence_db
```

Then update the `.env` file with your database password.

### 3. Run Migrations

```bash
alembic upgrade head
```

This creates all the tables you need.

### 4. Start Everything

You'll need two terminal windows.

**Terminal 1** - Start the mock AI service:

```bash
python tests/mock_ai_service.py
```

This simulates the flaky external AI service (port 8001).

**Terminal 2** - Start the main API:

```bash
python run.py
```

API runs on port 8000. Visit http://localhost:8000/docs to see the interactive API docs.

### 5. Test It Out

Run the demo to see everything working:

```bash
python demo_full_requirements.py
```

Or run the integration tests:

```bash
pytest tests/test_concurrent_processing.py -v

# Expected: 4 passed
```

## Using the API

Send a call packet:

```bash
curl -X POST http://localhost:8000/v1/call/stream/call_123 \
  -H "Content-Type: application/json" \
  -d '{
    "sequence": 1,
    "data": "audio_metadata_packet_001",
    "timestamp": 1738411815.123
  }'
```

You'll get back something like:
```json
{
  "call_id": "call_123",
  "sequence": 1,
  "status": "accepted",
  "received_at": "2026-02-03T12:30:15.123Z",
  "response_time_ms": 12.5
}
```

Check call status:

```bash
curl http://localhost:8000/v1/call/call_123
```

Connect via WebSocket for real-time updates:

```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/supervisor/super_123');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Got update:', data.event_type, data);
};
```

Interactive docs are at http://localhost:8000/docs

## Testing

I wrote integration tests to make sure concurrent requests don't break things:

- Sending duplicate packets → only one gets inserted (idempotency works)
- Two adjacent packets arriving at the same time → both succeed (no race conditions)
- 50 concurrent requests → no lost updates (optimistic locking works)
- Concurrent state changes → consistent final state

Run them with:

```bash
pytest tests/test_concurrent_processing.py -v
```

## Performance

Response times are around 20ms on average, well under the 50ms target. I've tested it with 50+ concurrent requests without issues. Connection pool is set to 20 with 10 overflow.

## Random Notes

A few things to know:

- If packets arrive out of order, I log a warning but keep processing
- Optimistic locking handles concurrent updates (retries up to 3 times)
- There's a background worker that retries failed AI tasks every 5 minutes
- WebSocket connections have a 30s heartbeat to stay alive

Some useful SQL queries for debugging:

```sql
-- See what state calls are in
SELECT call_id, state, ai_status, version FROM calls;

-- Check packet sequence
SELECT call_id, sequence_number, created_at FROM call_events ORDER BY created_at;

-- Look at AI results
SELECT call_id, status, transcript FROM ai_results;
```
