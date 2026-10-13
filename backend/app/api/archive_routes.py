"""HTTP API архива станции: сводка серий для выбора окна плейбэка.

Только чтение и только сводка: сама загрузка окна — ручка плейбэка
(POST /api/playback/load_archive), а запись в архив невозможна in-принципе
(соединение ``file:…?mode=ro``).
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.services.playback.archive_reader import ArchiveError, list_series

router = APIRouter(prefix="/api/archive", tags=["archive"])


@router.get("/series")
async def series() -> Dict[str, Any]:
    """Серии по сигналу (серии одной партии слиты) и диапазон архива."""
    try:
        items, meta = await asyncio.to_thread(list_series, settings.ARCHIVE_PATH)
    except ArchiveError as exc:
        status_by_code = {"not_found": 404, "unreadable": 500}
        raise HTTPException(
            status_code=status_by_code.get(exc.code, 409),
            detail=exc.message,
        )
    return {
        "path": str(Path(settings.ARCHIVE_PATH).name),
        "observed_min_us": meta["observed_min_us"],
        "observed_max_us": meta["observed_max_us"],
        "total_rows": sum(item.rows for item in items),
        "series": [
            {
                "signal_id": item.signal_id,
                "value_type": item.value_type,
                "rows": item.rows,
                "observed_min_us": item.observed_min_us,
                "observed_max_us": item.observed_max_us,
            }
            for item in items
        ],
    }
