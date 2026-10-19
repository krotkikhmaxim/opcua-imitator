"""HTTP API пульта машины: состояние, воздействие, устранение, завершение.

Ручки намеренно узкие: один тест, одно активное воздействие. Взаимное
исключение с другими драйверами (сценарий, плейбэк) — через реестр драйверов:
пока тест идёт, чужой запуск отклоняется, а ручная запись блокируется
владением сигналов.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.drivers import DriverConflict, HeartbeatGate, ValueDriverRegistry
from app.services.machine import MachineError, MachineSession

router = APIRouter(prefix="/api/machine", tags=["machine"])

_ERROR_STATUS = {
    "unknown_state": 400,
    "unknown_event": 400,
    "not_started": 409,
    "already_started": 409,
    "not_applicable": 409,
    "no_active_event": 409,
    "event_active": 409,
    "busy": 409,
    "finished": 409,
    "signals_busy": 409,
}


class StartRequest(BaseModel):
    state: str


class EventRequest(BaseModel):
    kind: str


class RestartRequest(BaseModel):
    state: Optional[str] = None


class _Holder:
    session: Optional[MachineSession] = None


_holder = _Holder()
_registry: Optional[ValueDriverRegistry] = None
_gate: Optional[HeartbeatGate] = None


def set_machine_service(session: Optional[MachineSession]) -> None:
    _holder.session = session


def set_registry(registry: Optional[ValueDriverRegistry]) -> None:
    global _registry
    _registry = registry


def set_gate(gate: Optional[HeartbeatGate]) -> None:
    global _gate
    _gate = gate


def _service() -> MachineSession:
    if _holder.session is None:
        raise HTTPException(status_code=503, detail="Пульт машины не инициализирован")
    return _holder.session


def _ensure_can_start() -> None:
    """Другой драйвер (сценарий, плейбэк) должен быть остановлен."""
    if _registry is None:
        return
    try:
        _registry.ensure_can_start("machine")
    except DriverConflict as exc:
        raise HTTPException(status_code=409, detail=exc.message)


async def _refresh_gate() -> None:
    if _gate is not None:
        await _gate.refresh()


async def _call(action):
    try:
        return await action()
    except MachineError as exc:
        raise HTTPException(status_code=_ERROR_STATUS.get(exc.code, 409), detail=exc.message)


@router.get("")
async def status() -> Dict[str, Any]:
    return await _service().status()


@router.post("/start")
async def start(req: StartRequest) -> Dict[str, Any]:
    _ensure_can_start()
    result = await _call(lambda: _service().start(req.state))
    await _refresh_gate()
    return result


@router.post("/event")
async def apply_event(req: EventRequest) -> Dict[str, Any]:
    return await _call(lambda: _service().event(req.kind))


@router.post("/remedy")
async def remedy() -> Dict[str, Any]:
    return await _call(lambda: _service().remedy())


@router.post("/finish")
async def finish() -> Dict[str, Any]:
    result = await _call(lambda: _service().finish())
    await _refresh_gate()
    return result


@router.post("/restart")
async def restart(req: Optional[RestartRequest] = None) -> Dict[str, Any]:
    state = req.state if req is not None else None
    result = await _call(lambda: _service().restart(state))
    await _refresh_gate()
    return result
