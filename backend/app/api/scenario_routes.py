"""HTTP API сценариев: список, запуск, остановка, статус и журнал правды."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.drivers import DriverConflict, HeartbeatGate, ValueDriverRegistry
from app.services.scenario import ScenarioEngine, ScenarioError

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


class StartRequest(BaseModel):
    kinds: Optional[List[str]] = None
    mode: Optional[str] = None
    seed: Optional[int] = None
    once: bool = False


class _Holder:
    engine: Optional[ScenarioEngine] = None


_holder = _Holder()
_registry: Optional[ValueDriverRegistry] = None
_gate: Optional[HeartbeatGate] = None


def set_scenario_engine(engine: Optional[ScenarioEngine]) -> None:
    _holder.engine = engine


def get_scenario_engine() -> Optional[ScenarioEngine]:
    return _holder.engine


def set_registry(registry: Optional[ValueDriverRegistry]) -> None:
    global _registry
    _registry = registry


def set_gate(gate: Optional[HeartbeatGate]) -> None:
    global _gate
    _gate = gate


def ensure_manual_writes_allowed() -> None:
    """Пока активен драйвер (сценарий, плейбэк), ручная запись испортила бы его."""
    if _registry is not None:
        try:
            _registry.ensure_manual_writes_allowed()
        except DriverConflict as exc:
            raise HTTPException(status_code=409, detail=exc.message)
        return
    engine = _holder.engine
    if engine is not None and engine.running:
        raise HTTPException(
            status_code=409,
            detail="Идёт сценарий: ручная запись отключена, остановите сценарий",
        )


def _ensure_can_start(driver_id: str) -> None:
    if _registry is None:
        return
    try:
        _registry.ensure_can_start(driver_id)
    except DriverConflict as exc:
        raise HTTPException(status_code=409, detail=exc.message)


async def _after_activity_change() -> None:
    """После запуска/остановки драйвера — пересчитать шлюз heartbeat."""
    if _gate is not None:
        await _gate.refresh()


def _engine() -> ScenarioEngine:
    if _holder.engine is None:
        raise HTTPException(status_code=503, detail="Сценарии не инициализированы")
    return _holder.engine


@router.get("")
async def describe() -> Dict[str, Any]:
    return _engine().describe()


@router.get("/status")
async def status() -> Dict[str, Any]:
    return _engine().status()


@router.post("/start")
async def start(req: StartRequest) -> Dict[str, Any]:
    _ensure_can_start("scenario")
    try:
        result = await _engine().start(
            kinds=req.kinds, mode=req.mode, seed=req.seed, once=req.once
        )
    except ScenarioError as exc:
        code = 400 if exc.code in ("unknown_kind", "unknown_mode") else 409
        raise HTTPException(status_code=code, detail=exc.message)
    await _after_activity_change()
    return result


@router.post("/stop")
async def stop() -> Dict[str, Any]:
    result = await _engine().stop()
    await _after_activity_change()
    return result


@router.get("/journal")
async def journal(limit: int = Query(default=50, ge=1, le=200)) -> List[Dict[str, Any]]:
    return _engine().journal(limit)
