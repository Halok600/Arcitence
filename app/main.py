"""FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.call import router as call_router
from app.api.v1.websocket import router as websocket_router
from app.core.config import settings
from app.core.database import engine

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    
    Handles:
    - Database connection pool initialization
    - Background retry worker startup
    - Cleanup on shutdown
    """
    logger.info("Starting PBX microservice...")
    logger.info(f"Environment: {settings.app_env}")
    logger.info(f"Database: {settings.database_url.unicode_string()}")

    # Startup: connection pool created automatically by SQLAlchemy
    
    # Start periodic retry worker for failed AI tasks
    from app.services.retry_worker import start_retry_worker, stop_retry_worker
    from app.core.websocket_manager import websocket_manager, heartbeat_worker
    
    retry_task = await start_retry_worker()
    
    # Start WebSocket heartbeat worker
    heartbeat_task = asyncio.create_task(heartbeat_worker())
    
    logger.info("Background workers started (retry worker + WebSocket heartbeat)")
    
    yield

    # Shutdown: cleanup
    logger.info("Shutting down...")
    
    # Stop retry worker gracefully
    await stop_retry_worker()
    
    # Cancel heartbeat worker
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    
    # Close all WebSocket connections
    await websocket_manager.close_all()
    
    logger.info("Background workers stopped")
    
    await engine.dispose()
    logger.info("Database connections closed")


# Create FastAPI application
app = FastAPI(
    title="PBX Microservice",
    description="Production-grade call processing with async PostgreSQL and AI integration",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware (configure for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(call_router, prefix="/v1")
app.include_router(websocket_router, prefix="/v1")


@app.get("/health", tags=["health"])
async def health_check():
    """
    Health check endpoint.
    
    Used by:
    - Load balancers
    - Kubernetes liveness probe
    - Monitoring systems
    """
    return {
        "status": "healthy",
        "service": "pbx-microservice",
        "version": "0.1.0",
    }


@app.get("/", tags=["root"])
async def root():
    """Root endpoint with API information."""
    return {
        "service": "PBX Microservice",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app_env == "development",
        log_level=settings.log_level.lower(),
    )
