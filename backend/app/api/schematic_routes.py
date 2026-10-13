"""Мост «обозначение Э3 → сигналы стенда»: read-only раздача артефакта.

Артефакт строит ``scripts/build_schematic_bridge.py`` (мимо оператора:
запуск скрипта — шаг обновления стенда, не ручка API). Маршрут только
читает файл и отдаёт как есть; файла нет или он нечитаем — честный 404/500
с кодом, а не пустой мост: пустой мост неотличим от схемы без аппаратов.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.config import settings

router = APIRouter(prefix="/api/schematic", tags=["schematic"])


@router.get("/bridge")
def bridge() -> dict[str, Any]:
    path = Path(settings.SCHEMATIC_BRIDGE_PATH)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "code": "bridge_not_built",
                "message": "Артефакта моста нет — соберите его: "
                "scripts/build_schematic_bridge.py",
            },
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "bridge_unreadable", "message": f"Артефакт нечитаем: {exc}"},
        )
