"""
WebSocket API endpoints for supervisor dashboard.

Provides real-time updates for:
- Call events (packet ingestion)
- State changes
- AI processing results
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel

from app.core.websocket_manager import websocket_manager


logger = logging.getLogger(__name__)
router = APIRouter()


class WebSocketAuth(BaseModel):
    """Authentication payload for WebSocket connection."""
    supervisor_id: str
    token: Optional[str] = None  # Future: JWT token


@router.websocket("/ws/supervisor/{supervisor_id}")
async def websocket_supervisor_endpoint(
    websocket: WebSocket,
    supervisor_id: str,
    agent_id: Optional[str] = Query(None, description="Filter by agent"),
    call_id: Optional[str] = Query(None, description="Filter by call")
):
    """
    WebSocket endpoint for supervisor dashboard.
    
    Provides real-time updates for call events, state changes, and AI results.
    
    Query Parameters:
    - agent_id: Optional filter to only receive events for specific agent
    - call_id: Optional filter to only receive events for specific call
    
    Message Format:
    {
        "event_type": "PACKET_INGESTED" | "STATE_CHANGED" | "AI_COMPLETED" | "AI_FAILED",
        "timestamp": "2026-02-01T10:30:45.123456",
        "call_id": "call_123",
        "agent_id": "agent_001",
        "data": { ... }
    }
    
    Example Usage (JavaScript):
        const ws = new WebSocket('ws://localhost:8000/api/v1/ws/supervisor/super_123');
        
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log(`Event: ${data.event_type}`, data);
        };
        
        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };
        
        ws.onclose = () => {
            console.log('WebSocket closed');
        };
    
    Example with filters:
        // Only receive events for agent_001
        const ws = new WebSocket('ws://localhost:8000/api/v1/ws/supervisor/super_123?agent_id=agent_001');
        
        // Only receive events for call_123
        const ws = new WebSocket('ws://localhost:8000/api/v1/ws/supervisor/super_123?call_id=call_123');
    """
    filters = {}
    if agent_id:
        filters["agent_id"] = agent_id
    if call_id:
        filters["call_id"] = call_id
    
    logger.info(
        f"WebSocket connection attempt: supervisor={supervisor_id}, filters={filters}",
        extra={
            "supervisor_id": supervisor_id,
            "filters": filters
        }
    )
    
    try:
        # Register connection
        await websocket_manager.connect(websocket, supervisor_id, filters)
        
        # Keep connection alive and handle client messages
        while True:
            try:
                # Receive messages from client (for ping/pong or commands)
                data = await websocket.receive_text()
                
                # Handle client messages
                if data == "ping":
                    await websocket.send_json({
                        "event_type": "PONG",
                        "timestamp": asyncio.get_event_loop().time()
                    })
                elif data == "stats":
                    # Send connection statistics
                    stats = websocket_manager.get_stats()
                    await websocket.send_json({
                        "event_type": "STATS",
                        "timestamp": asyncio.get_event_loop().time(),
                        "data": stats
                    })
                else:
                    logger.debug(
                        f"Received message from {supervisor_id}: {data[:100]}"
                    )
            
            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected: supervisor={supervisor_id}")
                break
            
            except Exception as e:
                logger.error(
                    f"Error in WebSocket loop for {supervisor_id}: {e}",
                    exc_info=True
                )
                break
    
    finally:
        # Clean up connection
        await websocket_manager.disconnect(supervisor_id)


@router.get("/ws/stats")
async def websocket_stats():
    """
    Get WebSocket connection statistics.
    
    Returns:
        Active connections, total messages sent, errors, etc.
        
    Example Response:
        {
            "active_connections": 5,
            "total_connections": 127,
            "total_messages_sent": 4582,
            "total_errors": 3,
            "supervisors": [
                {
                    "supervisor_id": "super_123",
                    "connected_at": "2026-02-01T10:00:00",
                    "filters": {"agent_id": "agent_001"}
                }
            ]
        }
    """
    return websocket_manager.get_stats()
