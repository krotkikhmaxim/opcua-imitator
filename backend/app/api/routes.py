from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

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


_holder = StateServiceHolder()


def set_services(bus, storage: StateStorage) -> None:
    _holder.bus = bus
    _holder.storage = storage


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
    try:
        data = _holder.storage.read(req.filename)
    except StateFileError as e:
        raise HTTPException(status_code=404, detail=str(e))

    signals = data.get("signals", {})
    errors: List[str] = []
    loaded_count = 0

    for signal_id, value in signals.items():
        cfg = _holder.bus.cache.get_config(signal_id)
        if cfg is None:
            errors.append(f"Сигнал {signal_id} не найден в конфигурации")
            continue
        if not cfg.get("writable", False):
            errors.append(f"Сигнал {signal_id} только для чтения")
            continue
        ok = await _holder.bus.write_value(signal_id, value)
        if ok:
            loaded_count += 1
        else:
            errors.append(f"Ошибка записи сигнала {signal_id}")

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
