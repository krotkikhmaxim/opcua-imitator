import logging
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router, set_services
from app.config import load_signal_config, settings
from app.services.opc_bus import OPCMode, OPCSignalBus
from app.services.signal_cache import SignalCache
from app.services.storage import StateStorage
from app.services.ws import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("opcua-imitator")

app = FastAPI(title="OPC UA State Imitator", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_configs = load_signal_config(settings.SIGNAL_CONFIG_PATH)
_cache = SignalCache(_configs)
_bus = OPCSignalBus(_cache, mode=os.getenv("OPC_UA_MODE", OPCMode.IMITATOR))
_storage = StateStorage(settings.SAVED_STATES_DIR)

set_services(_bus, _storage)
app.include_router(router)


@app.on_event("startup")
async def startup() -> None:
    await _bus.connect()
    logger.info("OPC UA bus started in %s mode", _bus.mode)


@app.on_event("shutdown")
async def shutdown() -> None:
    await _bus.close()


@app.get("/api/signals")
async def get_signals() -> dict:
    from typing import Any, Dict

    signals: Dict[str, Any] = {}
    for cfg in _cache.config:
        signals[cfg["id"]] = {
            "id": cfg["id"],
            "name": cfg.get("name", cfg["id"]),
            "type": cfg.get("type", "string"),
            "writable": cfg.get("writable", False),
            "value": _cache.get_value(cfg["id"]),
        }
    return {"signals": signals}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


def _serve_frontend(app: FastAPI) -> None:
    dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/")
        async def index():
            from fastapi.responses import FileResponse
            return FileResponse(str(dist / "index.html"))


_serve_frontend(app)
