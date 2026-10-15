"""HTTP API сценариев: список, запуск, остановка, статус и журнал правды."""

from typing import Any, AbstractSet, Callable, Dict, List, Optional

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


class RunRequest(BaseModel):
    """Указание прогона; без него — единственный активный (несколько — 409)."""

    run_id: Optional[str] = None


_ERROR_STATUS = {
    "unknown_kind": 400,
    "unknown_mode": 400,
    "unknown_run": 404,
    "not_running": 409,
    "ambiguous_run": 409,
    "signals_busy": 409,
    "real_mode": 409,
}


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


_stop_playback: Optional[Callable[[], None]] = None


def set_playback_stop(stop: Optional[Callable[[], None]]) -> None:
    """Хук автостопа плейбэка: применение сценария останавливает таймлайн."""
    global _stop_playback
    _stop_playback = stop


def ensure_manual_writes_allowed(exclude: Optional[AbstractSet[str]] = None) -> None:
    """Грубая блокировка ручной записи активными драйверами.

    Сценарий исключён (ТЗ #5 §9.2): за него отвечает владение по-сигнально —
    механизм применения отклонит запись в занятый сигнал (код ``owned``),
    а сигналы вне набора прогона ручная правка менять может. Плейбэк остаётся
    грубым запретом: его окно данных не выражается набором сигналов.
    """
    if _registry is not None:
        try:
            _registry.ensure_manual_writes_allowed(exclude=exclude)
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


async def refresh_activity_gate() -> None:
    """Публичный хелпер для других драйверов (плейбэк): пересчитать шлюз."""
    await _after_activity_change()


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
    if _stop_playback is not None:
        _stop_playback()  # применение сценария/режима останавливает таймлайн
    _ensure_can_start("scenario")
    try:
        result = await _engine().start(
            kinds=req.kinds, mode=req.mode, seed=req.seed, once=req.once
        )
    except ScenarioError as exc:
        raise HTTPException(status_code=_ERROR_STATUS.get(exc.code, 409), detail=exc.message)
    await _after_activity_change()
    return result


@router.post("/stop")
async def stop(req: Optional[RunRequest] = None) -> Dict[str, Any]:
    result = await _engine().stop(req.run_id if req is not None else None)
    await _after_activity_change()
    return result


@router.post("/pause")
async def pause(req: Optional[RunRequest] = None) -> Dict[str, Any]:
    """Пауза прогона: смены замирают, владение сохраняется (ТЗ #5 §9.1)."""
    try:
        result = await _engine().pause(req.run_id if req is not None else None)
    except ScenarioError as exc:
        raise HTTPException(status_code=_ERROR_STATUS.get(exc.code, 409), detail=exc.message)
    return result


@router.post("/resume")
async def resume(req: Optional[RunRequest] = None) -> Dict[str, Any]:
    """Продолжить паузу; интервалы сценария остаются точными (ТЗ #5 §9.1)."""
    try:
        result = await _engine().resume(req.run_id if req is not None else None)
    except ScenarioError as exc:
        raise HTTPException(status_code=_ERROR_STATUS.get(exc.code, 409), detail=exc.message)
    return result


@router.get("/journal")
async def journal(limit: int = Query(default=50, ge=1, le=200)) -> List[Dict[str, Any]]:
    return _engine().journal(limit)
