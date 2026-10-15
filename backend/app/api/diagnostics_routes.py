"""Чтение журнала диагностики (ТЗ #5, шаг 9 — §14).

Только чтение, хвост журнала, новые первыми. Журнал пишут операции
применения и жизненный цикл сценариев; рост ограничен вытеснением.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Query

from app.services.diagnostics import DiagnosticsJournal

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

_journal: DiagnosticsJournal = DiagnosticsJournal()


def set_diagnostics_journal(journal: DiagnosticsJournal) -> None:
    """Журнал ставится при старте приложения (один на процесс)."""
    global _journal
    _journal = journal


@router.get("/events")
async def events(limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
    """Последние записи диагностики: операции и события сценариев."""
    found: List[Dict[str, Any]] = _journal.tail(limit)
    return {"events": found, "total": len(_journal)}
