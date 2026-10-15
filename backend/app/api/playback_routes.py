"""HTTP API плейбэка трендов: загрузка, транспорт, статус.

Большой файл грузится по пути (сервер-сайд, без копирования по HTTP);
multipart — для небольших файлов. Управление одной ручкой «control»,
ошибки домена переводятся в HTTP: занят/не идёт — 409, плохой аргумент —
400, файл не найден — 404, слишком велик — 413.
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.api.scenario_routes import refresh_activity_gate
from app.services.playback.service import PlaybackError, PlaybackService

router = APIRouter(prefix="/api/playback", tags=["playback"])

_service: Optional[PlaybackService] = None
_get_configs: Callable[[], Dict[str, Dict[str, Any]]] = dict
_upload_dir = Path("playback_uploads")


def set_playback_service(
    service: Optional[PlaybackService],
    get_configs: Callable[[], Dict[str, Dict[str, Any]]],
    upload_dir: Optional[Path] = None,
) -> None:
    global _service, _get_configs, _upload_dir
    _service = service
    _get_configs = get_configs
    if upload_dir is not None:
        _upload_dir = upload_dir


def _service_required() -> PlaybackService:
    if _service is None:
        raise HTTPException(status_code=503, detail="Плейбэк не инициализирован")
    return _service


def _translate(exc: PlaybackError) -> HTTPException:
    code_by_reason = {
        "not_found": 404,
        "too_large": 413,
        "too_many_changes": 413,
        "no_paths": 400,
        "no_series": 400,
        "unknown_signals": 400,
        "bad_window": 400,
        "empty_window": 400,
        "unreadable": 500,
        "bad_rate": 400,
        "bad_position": 400,
        "frozen": 409,
    }
    status = code_by_reason.get(exc.code, 409)
    return HTTPException(status_code=status, detail=exc.message)


class LoadByPathRequest(BaseModel):
    path: str


class LoadArchiveRequest(BaseModel):
    """Окно архива станции: сигналы (None — все) и диапазон observed_us."""

    signal_ids: Optional[list[str]] = None
    start_us: Optional[int] = None
    end_us: Optional[int] = None


class ControlRequest(BaseModel):
    action: str
    rate: Optional[float] = None
    seek_ms: Optional[int] = None
    # Участок прогона для play: от начала тренда (мс) до конца (включительно).
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


@router.get("/status")
async def status() -> Dict[str, Any]:
    return _service_required().status()


@router.post("/load_path")
async def load_by_path(req: LoadByPathRequest) -> Dict[str, Any]:
    try:
        return _service_required().load(req.path, _get_configs(), max_bytes=_max_bytes())
    except PlaybackError as exc:
        raise _translate(exc)


@router.post("/load")
async def load_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    from app.config import settings

    data = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > settings.PLAYBACK_UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Файл больше лимита загрузки; используйте загрузку по пути",
            )
    _upload_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file.filename or "upload.xlsx").name)
    if not safe:
        safe = "upload.xlsx"
    target = _upload_dir / safe
    target.write_bytes(bytes(data))
    return _service_required().load(str(target), _get_configs(), max_bytes=_max_bytes())


@router.post("/load_archive")
async def load_archive(req: LoadArchiveRequest) -> Dict[str, Any]:
    """Окно SQLite-архива станции в тот же плейбэк (source=архив).

    Открытие окна (сводный COUNT по 467 МБ) — вне цикла событий: поток
    запросов API не должен замерзать на агрегате sqlite.
    """
    import asyncio

    from app.config import settings

    service = _service_required()
    try:
        return await asyncio.to_thread(
            service.load_archive,
            settings.ARCHIVE_PATH,
            signal_ids=req.signal_ids,
            start_us=req.start_us,
            end_us=req.end_us,
            max_changes=settings.PLAYBACK_MAX_CHANGES,
        )
    except PlaybackError as exc:
        raise _translate(exc)


@router.post("/control")
async def control(req: ControlRequest) -> Dict[str, Any]:
    service = _service_required()
    try:
        if req.action == "play":
            result = service.play(start_ms=req.start_ms or 0, end_ms=req.end_ms)
        elif req.action == "pause":
            result = service.pause()
        elif req.action == "resume":
            result = service.resume()
        elif req.action == "stop":
            result = service.stop()
        elif req.action == "rate":
            if req.rate is None:
                raise HTTPException(status_code=400, detail="Для rate нужно значение скорости")
            result = service.set_rate(req.rate)
        elif req.action == "seek":
            if req.seek_ms is None:
                raise HTTPException(status_code=400, detail="Для seek нужно seek_ms")
            result = await service.seek(req.seek_ms)
        else:
            raise HTTPException(status_code=400, detail=f"Неизвестное действие «{req.action}»")
    except PlaybackError as exc:
        raise _translate(exc)
    # шлюз heartbeat пересчитывается сразу, не дожидаясь тика фида
    await refresh_activity_gate()
    return result


@router.post("/unload")
async def unload() -> Dict[str, Any]:
    try:
        return _service_required().unload()
    except PlaybackError as exc:
        raise _translate(exc)


def _max_bytes() -> int:
    from app.config import settings

    return settings.PLAYBACK_MAX_BYTES
