"""WebSocket manager for Caging - real-time updates."""
import asyncio
import json
import time
from typing import Set, Optional

try:
    from fastapi import WebSocket
except ImportError:
    WebSocket = None


class WebSocketManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: Set[WebSocket] = set()
        self._heartbeat_interval: int = 30

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self._connections.discard(websocket)

    async def broadcast(self, message: dict):
        """Send message to all connected clients."""
        disconnected = set()
        for ws in self._connections:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.add(ws)
        self._connections -= disconnected

    async def send_to(self, websocket: WebSocket, message: dict):
        """Send message to a specific client."""
        try:
            await websocket.send_json(message)
        except Exception:
            self.disconnect(websocket)

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def heartbeat_loop(self):
        """Periodic heartbeat to keep connections alive."""
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            await self.broadcast({"type": "heartbeat", "timestamp": time.time()})


# Singleton instance
ws_manager = WebSocketManager()
