import json
from typing import List

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        for websocket in list(self.active):
            try:
                await websocket.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                self.disconnect(websocket)


manager = ConnectionManager()
