"""
WebSocket Connection Manager for Supervisor Dashboard.

Manages multiple concurrent WebSocket connections with:
- Thread-safe connection tracking
- Broadcast to all supervisors
- Selective filtering (by agent, call, etc.)
- Automatic cleanup on disconnect
- Heartbeat/keepalive support
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Set, Optional, Any
from collections import defaultdict

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel


logger = logging.getLogger(__name__)


class WebSocketMessage(BaseModel):
    """
    Standard message format for WebSocket events.
    
    All events sent to supervisors follow this schema for consistency.
    """
    event_type: str  # PACKET_INGESTED, STATE_CHANGED, AI_COMPLETED, AI_FAILED
    timestamp: str
    call_id: str
    agent_id: Optional[str] = None
    data: Dict[str, Any]


class ConnectionInfo(BaseModel):
    """Metadata about a WebSocket connection."""
    websocket: WebSocket
    supervisor_id: str
    connected_at: datetime
    filters: Dict[str, Any] = {}  # Optional filters (agent_id, call_id, etc.)
    
    class Config:
        arbitrary_types_allowed = True


class WebSocketManager:
    """
    Manages WebSocket connections for supervisor dashboard.
    
    Features:
    - Multiple concurrent supervisors
    - Broadcast to all or filtered subset
    - Automatic reconnection handling
    - Connection pooling
    - Heartbeat monitoring
    
    Thread-safe: Uses asyncio locks for connection management.
    """
    
    def __init__(self):
        # Active connections: supervisor_id → ConnectionInfo
        self._connections: Dict[str, ConnectionInfo] = {}
        
        # Reverse index: call_id → set of supervisor_ids
        self._call_subscriptions: Dict[str, Set[str]] = defaultdict(set)
        
        # Reverse index: agent_id → set of supervisor_ids
        self._agent_subscriptions: Dict[str, Set[str]] = defaultdict(set)
        
        # Lock for thread-safe operations
        self._lock = asyncio.Lock()
        
        # Statistics
        self._total_connections = 0
        self._total_messages_sent = 0
        self._total_errors = 0
    
    async def connect(
        self, 
        websocket: WebSocket, 
        supervisor_id: str,
        filters: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Register a new WebSocket connection.
        
        Args:
            websocket: FastAPI WebSocket instance
            supervisor_id: Unique identifier for supervisor
            filters: Optional filters (agent_id, call_id, etc.)
            
        Example:
            await manager.connect(websocket, "supervisor_123", {"agent_id": "agent_001"})
        """
        await websocket.accept()
        
        async with self._lock:
            # Store connection
            connection_info = ConnectionInfo(
                websocket=websocket,
                supervisor_id=supervisor_id,
                connected_at=datetime.utcnow(),
                filters=filters or {}
            )
            self._connections[supervisor_id] = connection_info
            
            # Update subscriptions
            if filters:
                if "call_id" in filters:
                    self._call_subscriptions[filters["call_id"]].add(supervisor_id)
                if "agent_id" in filters:
                    self._agent_subscriptions[filters["agent_id"]].add(supervisor_id)
            
            self._total_connections += 1
        
        logger.info(
            f"WebSocket connected: supervisor={supervisor_id}, "
            f"filters={filters}, total_connections={len(self._connections)}",
            extra={
                "supervisor_id": supervisor_id,
                "filters": filters,
                "total_connections": len(self._connections)
            }
        )
        
        # Send welcome message
        await self._send_to_supervisor(
            supervisor_id,
            {
                "event_type": "CONNECTION_ESTABLISHED",
                "timestamp": datetime.utcnow().isoformat(),
                "call_id": "system",
                "data": {
                    "supervisor_id": supervisor_id,
                    "filters": filters,
                    "message": "Connected to supervisor dashboard"
                }
            }
        )
    
    async def disconnect(self, supervisor_id: str) -> None:
        """
        Unregister a WebSocket connection.
        
        Args:
            supervisor_id: Unique identifier for supervisor
        """
        async with self._lock:
            connection_info = self._connections.pop(supervisor_id, None)
            
            if connection_info:
                # Clean up subscriptions
                filters = connection_info.filters
                if "call_id" in filters:
                    self._call_subscriptions[filters["call_id"]].discard(supervisor_id)
                if "agent_id" in filters:
                    self._agent_subscriptions[filters["agent_id"]].discard(supervisor_id)
                
                logger.info(
                    f"WebSocket disconnected: supervisor={supervisor_id}, "
                    f"total_connections={len(self._connections)}",
                    extra={
                        "supervisor_id": supervisor_id,
                        "total_connections": len(self._connections)
                    }
                )
    
    async def broadcast(
        self, 
        event_type: str, 
        call_id: str,
        data: Dict[str, Any],
        agent_id: Optional[str] = None
    ) -> int:
        """
        Broadcast event to all connected supervisors.
        
        Args:
            event_type: Type of event (PACKET_INGESTED, STATE_CHANGED, etc.)
            call_id: Call identifier
            data: Event payload
            agent_id: Optional agent identifier for filtering
            
        Returns:
            Number of supervisors notified
            
        Example:
            await manager.broadcast(
                "STATE_CHANGED",
                "call_123",
                {"from": "IN_PROGRESS", "to": "COMPLETED"}
            )
        """
        message = {
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "call_id": call_id,
            "agent_id": agent_id,
            "data": data
        }
        
        # Find all supervisors that should receive this message
        target_supervisors = await self._find_target_supervisors(call_id, agent_id)
        
        # Send to all targets (non-blocking)
        send_tasks = [
            self._send_to_supervisor(supervisor_id, message)
            for supervisor_id in target_supervisors
        ]
        
        results = await asyncio.gather(*send_tasks, return_exceptions=True)
        
        # Count successes
        success_count = sum(1 for r in results if r is True)
        error_count = sum(1 for r in results if isinstance(r, Exception))
        
        if error_count > 0:
            logger.warning(
                f"Broadcast errors: {error_count}/{len(send_tasks)} failed",
                extra={
                    "event_type": event_type,
                    "call_id": call_id,
                    "errors": error_count
                }
            )
        
        self._total_messages_sent += success_count
        self._total_errors += error_count
        
        logger.debug(
            f"Broadcast: {event_type} for {call_id} → {success_count} supervisors",
            extra={
                "event_type": event_type,
                "call_id": call_id,
                "recipients": success_count
            }
        )
        
        return success_count
    
    async def _find_target_supervisors(
        self, 
        call_id: str, 
        agent_id: Optional[str]
    ) -> Set[str]:
        """
        Find supervisors that should receive this event based on filters.
        
        Logic:
        1. Supervisors with no filters → receive all events
        2. Supervisors filtered by call_id → only matching calls
        3. Supervisors filtered by agent_id → only matching agents
        
        Args:
            call_id: Call identifier
            agent_id: Optional agent identifier
            
        Returns:
            Set of supervisor_ids
        """
        async with self._lock:
            target_supervisors = set()
            
            for supervisor_id, connection_info in self._connections.items():
                filters = connection_info.filters
                
                # No filters = receive everything
                if not filters:
                    target_supervisors.add(supervisor_id)
                    continue
                
                # Check call_id filter
                if "call_id" in filters:
                    if filters["call_id"] == call_id:
                        target_supervisors.add(supervisor_id)
                    continue
                
                # Check agent_id filter
                if "agent_id" in filters and agent_id:
                    if filters["agent_id"] == agent_id:
                        target_supervisors.add(supervisor_id)
                    continue
            
            return target_supervisors
    
    async def _send_to_supervisor(
        self, 
        supervisor_id: str, 
        message: Dict[str, Any]
    ) -> bool:
        """
        Send message to a specific supervisor.
        
        Args:
            supervisor_id: Supervisor identifier
            message: Message payload
            
        Returns:
            True if sent successfully, False otherwise
        """
        connection_info = self._connections.get(supervisor_id)
        
        if not connection_info:
            return False
        
        try:
            await connection_info.websocket.send_json(message)
            return True
            
        except WebSocketDisconnect:
            logger.warning(f"Supervisor {supervisor_id} disconnected during send")
            await self.disconnect(supervisor_id)
            return False
            
        except Exception as e:
            logger.error(
                f"Error sending to supervisor {supervisor_id}: {e}",
                exc_info=True,
                extra={"supervisor_id": supervisor_id, "error": str(e)}
            )
            return False
    
    async def send_heartbeat(self) -> None:
        """
        Send heartbeat/keepalive to all connections.
        
        Should be called periodically (e.g., every 30 seconds) to:
        - Detect dead connections
        - Keep connections alive through proxies/load balancers
        """
        heartbeat_message = {
            "event_type": "HEARTBEAT",
            "timestamp": datetime.utcnow().isoformat(),
            "call_id": "system",
            "data": {
                "connections": len(self._connections),
                "uptime": "ok"
            }
        }
        
        async with self._lock:
            supervisor_ids = list(self._connections.keys())
        
        for supervisor_id in supervisor_ids:
            await self._send_to_supervisor(supervisor_id, heartbeat_message)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get connection statistics.
        
        Returns:
            Dict with connection counts and metrics
        """
        return {
            "active_connections": len(self._connections),
            "total_connections": self._total_connections,
            "total_messages_sent": self._total_messages_sent,
            "total_errors": self._total_errors,
            "call_subscriptions": len(self._call_subscriptions),
            "agent_subscriptions": len(self._agent_subscriptions),
            "supervisors": [
                {
                    "supervisor_id": info.supervisor_id,
                    "connected_at": info.connected_at.isoformat(),
                    "filters": info.filters
                }
                for info in self._connections.values()
            ]
        }
    
    async def close_all(self) -> None:
        """
        Close all WebSocket connections gracefully.
        
        Called during application shutdown.
        """
        logger.info(f"Closing {len(self._connections)} WebSocket connections")
        
        async with self._lock:
            supervisor_ids = list(self._connections.keys())
        
        for supervisor_id in supervisor_ids:
            connection_info = self._connections.get(supervisor_id)
            if connection_info:
                try:
                    await connection_info.websocket.close(code=1000, reason="Server shutdown")
                except Exception as e:
                    logger.error(f"Error closing connection {supervisor_id}: {e}")
            
            await self.disconnect(supervisor_id)
        
        logger.info("All WebSocket connections closed")


# Global WebSocket manager instance
websocket_manager = WebSocketManager()


async def heartbeat_worker():
    """
    Background worker to send periodic heartbeats.
    
    Runs every 30 seconds to keep connections alive.
    """
    while True:
        try:
            await asyncio.sleep(30)
            await websocket_manager.send_heartbeat()
        except asyncio.CancelledError:
            logger.info("Heartbeat worker cancelled")
            break
        except Exception as e:
            logger.error(f"Heartbeat worker error: {e}", exc_info=True)
