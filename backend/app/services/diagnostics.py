"""Диагностика операций и жизненного цикла (ТЗ #5, шаг 9 — §14).

Один ограниченный журнал для эксплуатации. Два вида записей:

- операция применения: идентификатор (op_id), источник, затронутые
  сигналы, результат, ошибки, длительность, ревизии;
- событие сценария: старт, отказ старта (с перечнем конфликтов владения),
  пауза, продолжение, остановка, завершение, ошибка (с шагом — эпизодом
  прогона), освобождение владения.

Высокочастотные успешные обновления сценария в журнал по одной не
попадают — прогон агрегирует их счётчиками writes/write_failures (§14
разрешает агрегировать); ошибки и события управления фиксируются всегда.
Рост ограничен: deque с вытеснением старейших, наружу — хвост.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Deque, Dict, List, Optional

#: Вместимость журнала диагностики; старейшие записи вытесняются.
DIAGNOSTICS_LIMIT = 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiagnosticsJournal:
    """Ограниченный журнал операций и событий; чтение — хвостом."""

    def __init__(self, limit: int = DIAGNOSTICS_LIMIT) -> None:
        self._events: Deque[Dict[str, Any]] = deque(maxlen=limit)

    def record(self, entry: Dict[str, Any]) -> None:
        """Записать событие (добавление в ограниченную очередь не падает)."""
        self._events.append({"ts": _now_iso(), **entry})

    def record_operation(
        self,
        *,
        op_id: str,
        source: str,
        status: str,
        signals: List[str],
        duration_ms: float,
        errors: Optional[List[str]] = None,
        revisions: Optional[Dict[str, int]] = None,
    ) -> None:
        """Запись об операции применения (§14): кто, что, чем кончилось."""
        self.record({
            "kind": "operation",
            "op_id": op_id,
            "source": source,
            "status": status,
            "signals": list(signals),
            "errors": list(errors or []),
            "duration_ms": round(duration_ms, 3),
            "revisions": dict(revisions or {}),
        })

    def record_scenario(self, event: str, **fields: Any) -> None:
        """Событие жизненного цикла сценария (§14): старт/пауза/…/ошибка."""
        self.record({"kind": "scenario", "event": event, **fields})

    def tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Последние записи, новые первыми."""
        return list(islice(reversed(self._events), max(0, limit)))

    def __len__(self) -> int:
        return len(self._events)
