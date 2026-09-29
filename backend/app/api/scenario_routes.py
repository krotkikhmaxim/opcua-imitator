"""HTTP API сценариев: список, запуск, остановка, статус и журнал правды."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

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


def set_scenario_engine(engine: Optional[ScenarioEngine]) -> None:
    _holder.engine = engine


def get_scenario_engine() -> Optional[ScenarioEngine]:
    return _holder.engine


def ensure_manual_writes_allowed() -> None:
    """Пока идёт сценарий, ручная запись испортила бы журнал правды."""
    engine = _holder.engine
    if engine is not None and engine.running:
        raise HTTPException(
            status_code=409,
            detail="Идёт сценарий: ручная запись отключена, остановите сценарий",
        )


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
    try:
        return await _engine().start(kinds=req.kinds, mode=req.mode, seed=req.seed, once=req.once)
    except ScenarioError as exc:
        code = 400 if exc.code in ("unknown_kind", "unknown_mode") else 409
        raise HTTPException(status_code=code, detail=exc.message)


@router.post("/stop")
async def stop() -> Dict[str, Any]:
    return await _engine().stop()


@router.get("/journal")
async def journal(limit: int = Query(default=50, ge=1, le=200)) -> List[Dict[str, Any]]:
    return _engine().journal(limit)
