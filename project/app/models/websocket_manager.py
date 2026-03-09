from typing import Dict, Set, List, Tuple
from fastapi import WebSocket
import json

# Chat room: (websocket, username, role)
ChatConnection = Tuple[WebSocket, str, str]

class ChatRoomManager:
    """Manages WebSocket connections for the group chatroom (managers + developers)."""
    
    def __init__(self):
        self.connections: List[ChatConnection] = []
    
    async def connect(self, websocket: WebSocket, username: str, role: str):
        await websocket.accept()
        self.connections.append((websocket, username, role))
    
    def disconnect(self, websocket: WebSocket):
        self.connections = [(ws, u, r) for (ws, u, r) in self.connections if ws != websocket]
    
    async def broadcast(self, message: dict, exclude_websocket: WebSocket = None):
        """Send message to all connected clients, optionally excluding one."""
        disconnected = []
        payload = json.dumps(message)
        for ws, _, _ in self.connections:
            if ws == exclude_websocket:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws)

class WebSocketManager:
    """Manages WebSocket connections for live code viewing"""
    
    def __init__(self):
        # Map of task_id -> set of manager WebSocket connections
        self.manager_connections: Dict[str, Set[WebSocket]] = {}
        # Map of task_id -> developer WebSocket connection
        self.developer_connections: Dict[str, WebSocket] = {}
    
    async def connect_manager(self, websocket: WebSocket, task_id: str):
        """Connect a manager to view live code for a task"""
        await websocket.accept()
        if task_id not in self.manager_connections:
            self.manager_connections[task_id] = set()
        self.manager_connections[task_id].add(websocket)
    
    async def disconnect_manager(self, websocket: WebSocket, task_id: str):
        """Disconnect a manager from viewing a task"""
        if task_id in self.manager_connections:
            self.manager_connections[task_id].discard(websocket)
            if not self.manager_connections[task_id]:
                del self.manager_connections[task_id]
    
    async def connect_developer(self, websocket: WebSocket, task_id: str):
        """Connect a developer to send code updates for a task"""
        await websocket.accept()
        # Disconnect previous connection if exists
        if task_id in self.developer_connections:
            old_ws = self.developer_connections[task_id]
            try:
                await old_ws.close()
            except:
                pass
        self.developer_connections[task_id] = websocket
    
    async def disconnect_developer(self, task_id: str):
        """Disconnect a developer from a task"""
        if task_id in self.developer_connections:
            try:
                await self.developer_connections[task_id].close()
            except:
                pass
            del self.developer_connections[task_id]
        
        # Notify all managers that developer disconnected
        if task_id in self.manager_connections:
            disconnected = set()
            message = json.dumps({"type": "developer_disconnected", "active": False})
            
            for websocket in self.manager_connections[task_id]:
                try:
                    await websocket.send_text(message)
                except:
                    disconnected.add(websocket)
            
            # Remove disconnected connections
            for ws in disconnected:
                self.manager_connections[task_id].discard(ws)
    
    async def broadcast_code_update(self, task_id: str, code: str):
        """Broadcast code update from developer to all watching managers"""
        if task_id in self.manager_connections:
            disconnected = set()
            message = json.dumps({"type": "code_update", "code": code})
            
            for websocket in self.manager_connections[task_id]:
                try:
                    await websocket.send_text(message)
                except:
                    disconnected.add(websocket)
            
            # Remove disconnected connections
            for ws in disconnected:
                self.manager_connections[task_id].discard(ws)
    
    def has_developer_connection(self, task_id: str) -> bool:
        """Check if a developer is connected for a task"""
        return task_id in self.developer_connections

# Global WebSocket manager instance
websocket_manager = WebSocketManager()

# Global chat room manager
chat_room_manager = ChatRoomManager()
