import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api.routes import router, set_services
from app.config import load_signal_config, settings
from app.services.heartbeat import HeartbeatService
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
_bus = OPCSignalBus(_cache, mode=os.getenv("OPC_UA_MODE", OPCMode.SERVER))
_storage = StateStorage(settings.SAVED_STATES_DIR)
# Heartbeat (cyclic refresh) создаётся в startup для режима SERVER; вне его —
# None, и API сообщает о недоступности режима.
_heartbeat: Optional[HeartbeatService] = None

set_services(_bus, _storage)
app.include_router(router)


@app.on_event("startup")
async def startup() -> None:
    await _bus.connect()
    global _heartbeat
    if _bus.embedded is not None:
        _heartbeat = HeartbeatService(
            _bus.embedded, [cfg["id"] for cfg in _cache.config]
        )
        logger.info("heartbeat service ready (%d signals)", len(_cache.config))
    logger.info("OPC UA bus started in %s mode", _bus.mode)


@app.on_event("shutdown")
async def shutdown() -> None:
    if _heartbeat is not None:
        await _heartbeat.stop()
    await _bus.close()


@app.get("/api/signals")
async def get_signals() -> dict:
    from typing import Any, Dict

    signals: Dict[str, Any] = {}
    for cfg in _cache.config:
        signals[cfg["id"]] = {
            "id": cfg["id"],
            "short_id": cfg["id"],
            "name": cfg.get("name", cfg["id"]),
            "type": cfg.get("type", "Boolean"),
            "writable": cfg.get("writable", False),
            "value": await _bus.read_value(cfg["id"]),
            "direction": cfg.get("direction"),
            "channel": cfg.get("channel"),
            "module": cfg.get("module"),
            "cabinet": cfg.get("cabinet"),
            "device": cfg.get("device"),
            "address": cfg.get("address") or cfg.get("node_id") or cfg["id"],
            "project_tag": cfg.get("project_tag") or cfg.get("address") or cfg["id"],
            "bit": cfg.get("bit"),
            "word_address": cfg.get("word_address"),
            "logic": cfg.get("logic"),
        }
    return {"signals": signals}


class WriteSignalRequest(BaseModel):
    id: str
    value: Any = None


class WriteBatchRequest(BaseModel):
    values: dict[str, Any] = {}


def _coerce_value(cfg: dict, value: Any) -> Any:
    """Coerce an API value to the signal's OPC UA type.

    The imitator accepts a boolean as a JSON bool or a str/0/1; integers must
    fit the declared width (Int16/UInt16) so a bad simulation value fails at
    the edge instead of wrapping silently.
    """
    t = cfg.get("type")
    if t == "Boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "да", "вкл")
        return bool(value)
    if t in ("Int16", "UInt16"):
        if value is None:
            raise HTTPException(status_code=400, detail="Некорректное числовое значение")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Некорректное числовое значение")
        if t == "UInt16" and not 0 <= number <= 65535:
            raise HTTPException(status_code=400, detail="Значение вне диапазона UInt16 (0..65535)")
        if t == "Int16" and not -32768 <= number <= 32767:
            raise HTTPException(status_code=400, detail="Значение вне диапазона Int16 (-32768..32767)")
        return number
    if t == "bool":
        return bool(value)
    if t in ("int", "float") and value is not None:
        try:
            return int(value) if t == "int" else float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Некорректное числовое значение")
    return value


@app.post("/api/signals/write")
async def write_signal(req: WriteSignalRequest) -> dict:
    cfg = _cache.get_config(req.id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Сигнал {req.id} не найден")
    if not cfg.get("writable", False):
        raise HTTPException(status_code=403, detail=f"Сигнал {req.id} только для чтения")

    value = _coerce_value(cfg, req.value)

    ok = await _bus.write_value(req.id, value)
    if not ok:
        raise HTTPException(status_code=500, detail="Ошибка записи сигнала")
    return {"status": "ok", "id": req.id, "value": value}


@app.post("/api/signals/write_batch")
async def write_batch(req: WriteBatchRequest) -> dict:
    errors: list[str] = []
    applied: list[str] = []
    for signal_id, value in req.values.items():
        cfg = _cache.get_config(signal_id)
        if cfg is None:
            errors.append(f"Сигнал {signal_id} не найден")
            continue
        if not cfg.get("writable", False):
            errors.append(f"Сигнал {signal_id} только для чтения")
            continue
        try:
            coerced = _coerce_value(cfg, value)
        except HTTPException as e:
            errors.append(f"Сигнал {signal_id}: {e.detail}")
            continue
        ok = await _bus.write_value(signal_id, coerced)
        if ok:
            applied.append(signal_id)
        else:
            errors.append(f"Ошибка записи сигнала {signal_id}")

    return {"status": "ok", "applied": applied, "errors": errors}


class HeartbeatRequest(BaseModel):
    enabled: bool


@app.get("/api/heartbeat")
async def get_heartbeat() -> dict:
    """Текущее состояние heartbeat-режима (cyclic refresh SourceTimestamp)."""
    if _heartbeat is None:
        return {"enabled": False, "supported": False}
    return {"enabled": _heartbeat.enabled, "supported": True}


@app.put("/api/heartbeat")
async def put_heartbeat(req: HeartbeatRequest) -> dict:
    """Включить/выключить heartbeat-режим (доступен только при OPC_UA_MODE=server)."""
    if _heartbeat is None:
        raise HTTPException(
            status_code=409,
            detail="Режим heartbeat доступен только при OPC_UA_MODE=server",
        )
    await _heartbeat.set_enabled(req.enabled)
    return {"enabled": _heartbeat.enabled, "supported": True}


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
