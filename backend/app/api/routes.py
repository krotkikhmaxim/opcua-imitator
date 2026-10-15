import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.scenario_routes import ensure_manual_writes_allowed
from app.config import settings
from app.services.storage import StateFileError, StateStorage
from app.services.ws import manager

router = APIRouter(prefix="/api/states", tags=["states"])


class SaveRequest(BaseModel):
    name: str
    description: Optional[str] = ""


class LoadRequest(BaseModel):
    filename: str


class StateServiceHolder:
    """Держатель глобальных сервисов, устанавливается при старте приложения."""

    bus = None
    storage: StateStorage = None
    # Журнал timestamped-записей (опционален): восстановление состояния —
    # тоже источник значений, сверка со станцией должна его видеть.
    ledger = None
    # Журнал диагностики (ТЗ #5 §14, опционален): загрузка состояния —
    # операция применения, попадает в один журнал с остальными.
    diagnostics = None


_holder = StateServiceHolder()


def set_services(bus, storage: StateStorage, ledger=None, diagnostics=None) -> None:
    _holder.bus = bus
    _holder.storage = storage
    _holder.ledger = ledger
    _holder.diagnostics = diagnostics


# Снимок состояния (ТЗ #5 §13) сохраняется и после успешной загрузки файла
# состояния: это тоже завершённая граница применения. Хук ставит main.py.
_snapshot_hook = None


def set_snapshot_hook(fn) -> None:
    global _snapshot_hook
    _snapshot_hook = fn


@router.get("/list")
async def list_states() -> List[Dict[str, Any]]:
    return _holder.storage.list_states()


@router.post("/save")
async def save_state(req: SaveRequest) -> Dict[str, Any]:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Введите название состояния")

    signals: Dict[str, Any] = {}
    for cfg in _holder.bus.cache.config:
        signal_id = cfg["id"]
        value = await _holder.bus.read_value(signal_id)
        if value is None:
            continue
        normalized = _holder.bus.cache.normalize(value)
        if isinstance(normalized, str) and normalized == "":
            continue
        signals[signal_id] = normalized

    filename = _holder.storage.save(name, req.description, signals, len(signals))
    return {"status": "ok", "filename": filename, "message": "Состояние успешно сохранено",
            "signal_count": len(signals)}


@router.post("/load")
async def load_state(req: LoadRequest) -> Dict[str, Any]:
    # Сценарий исключён из грубой блокировки (ТЗ #5 §9.2): его сигналы
    # защитит владение — apply_changes отклонит их с кодом owned, остальное
    # восстановится. Отчёт честный по каждому сигналу.
    ensure_manual_writes_allowed(exclude={"scenario"})
    try:
        data = _holder.storage.read(req.filename)
    except StateFileError as e:
        raise HTTPException(status_code=404, detail=str(e))

    signals = data.get("signals", {})
    # Через общий механизм (ТЗ #5 §6): проверка типа и прав, подтверждение
    # (ревизия/источник state_load). Восстановление состояния — не пакет
    # команд: применяется по сигнально с честным отчётом об ошибках.
    started = time.monotonic()
    result = await _holder.bus.apply_changes(
        signals,
        source="state_load",
        require_writable=True,
        contract="file",
    )
    if _holder.diagnostics is not None:
        revisions = {
            signal_id: result["results"][signal_id]["revision"]
            for signal_id in result["applied"]
            if "revision" in result["results"][signal_id]
        }
        _holder.diagnostics.record_operation(
            op_id=str(result.get("op_id") or "-"),
            source="state_load",
            status=str(result.get("status")),
            signals=list(signals),
            errors=list(result["errors"]),
            duration_ms=(time.monotonic() - started) * 1000.0,
            revisions=revisions,
        )
    loaded_count = len(result["applied"])
    errors = list(result["errors"])
    if _holder.ledger is not None:
        now = datetime.now(timezone.utc)
        for signal_id in result["applied"]:
            _holder.ledger.note(now, signal_id, result["results"][signal_id]["value"])
    if result["applied"] and _snapshot_hook is not None:
        _snapshot_hook()

    await manager.broadcast({
        "type": "state_loaded",
        "loaded_count": loaded_count,
        "errors": errors,
    })

    return {
        "status": "ok",
        "loaded_count": loaded_count,
        "errors": errors,
        "message": f"Загружено сигналов: {loaded_count}" +
                   (f", ошибок: {len(errors)}" if errors else ""),
    }


@router.delete("/delete/{filename}")
async def delete_state(filename: str) -> Dict[str, Any]:
    try:
        _holder.storage.delete(filename)
    except StateFileError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok", "message": "Состояние удалено"}


@router.post("/export")
async def export_state(req: SaveRequest) -> FileResponse:
    signals: Dict[str, Any] = {}
    for cfg in _holder.bus.cache.config:
        signal_id = cfg["id"]
        value = await _holder.bus.read_value(signal_id)
        if value is None:
            continue
        normalized = _holder.bus.cache.normalize(value)
        if isinstance(normalized, str) and normalized == "":
            continue
        signals[signal_id] = normalized

    name = req.name.strip() or "state"
    payload = _holder.storage.build_state(name, req.description, signals, len(signals))
    tmp = Path(settings.SAVED_STATES_DIR) / "export.json"
    tmp.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    filename = f"{_holder.storage.sanitize_name(name)}.json"
    return FileResponse(str(tmp), media_type="application/json", filename=filename)


@router.post("/import")
async def import_state(file: UploadFile = File(...)) -> Dict[str, Any]:
    raw = await file.read()
    if len(raw) > settings.MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Файл превышает максимальный размер 10 МБ")

    import json
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Неверный JSON формат")

    if not isinstance(data, dict) or "signals" not in data or not isinstance(data.get("signals"), dict):
        raise HTTPException(status_code=400, detail="Неверный JSON формат: отсутствует поле signals")

    name = str(data.get("name") or Path(file.filename or "import").stem)
    description = str(data.get("description") or "")
    signals = data.get("signals", {})

    if len(signals) > settings.MAX_SIGNAL_COUNT:
        raise HTTPException(status_code=400, detail="Слишком много сигналов")

    errors: List[str] = []
    for signal_id in signals:
        if _holder.bus.cache.get_config(signal_id) is None:
            errors.append(f"Сигнал {signal_id} не найден в конфигурации")

    filename = _holder.storage.save(name, description, signals, len(signals))
    return {"status": "ok", "filename": filename, "signal_count": len(signals),
            "errors": errors, "message": "Состояние импортировано"}
