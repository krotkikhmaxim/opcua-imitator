"""Журнал timestamped-записей: ожидаемое состояние имитатора на любой момент.

Сценарный движок и плейбэк пишут в шину через один writer с SourceTimestamp
момента своей шкалы, ручная запись — без метки. Журнал оборачивает writer
(и принимает отметки ручных записей) и умеет отвечать на вопрос «какое
значение должно быть у каждого сигнала в момент T»: сценарий стартует с
базиса, дальше — последнее записанное значение не позднее маркера.

Это наблюдатель: движок не знает о нём ничего, писатель передаётся ему из
``main.py`` уже обёрнутым, детерминизм шкалы не задевается.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Awaitable, Callable, Deque, Dict, Mapping, Optional, Tuple

Writer = Callable[[str, Any, datetime], Awaitable[bool]]


class WriteLedger:
    """Копит успешные записи; отвечает ожидаемым состоянием на момент."""

    def __init__(self, limit: int = 200_000) -> None:
        self._entries: Deque[Tuple[datetime, str, Any]] = deque(maxlen=limit)

    def wrap(self, writer: Writer) -> Writer:
        """Обернуть writer шины: успешная запись попадает в журнал."""

        async def wrapped(signal_id: str, value: Any, when: datetime) -> bool:
            ok = await writer(signal_id, value, when)
            if ok:
                self._entries.append((when, signal_id, value))
            return ok

        return wrapped

    def note(self, when: datetime, signal_id: str, value: Any) -> None:
        """Отметить ручную запись (у неё нет SourceTimestamp — берём «сейчас»)."""
        self._entries.append((when, signal_id, value))

    def clear(self) -> None:
        self._entries.clear()

    def expected_state_at(
        self, marker: datetime, baseline: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Ожидаемое состояние всех сигналов на ``marker``.

        Базис — исправное состояние модели (``hoist.BASELINE``); поверх —
        записи журнала не позднее маркера. Сигналы вне базиса (запись мимо
        опубликованного списка) не возвращаются: сверять их не с чем.
        """
        replayed: Dict[str, Any] = {}
        for when, signal_id, value in self._entries:
            if when <= marker:
                replayed[signal_id] = value
        state: Dict[str, Any] = {}
        for signal_id, default in baseline.items():
            state[signal_id] = replayed.get(signal_id, default)
        return state

    def last_write(self, signal_id: str) -> Optional[Tuple[datetime, Any]]:
        """Последняя запись сигнала (момент, значение) или None."""
        for when, sid, value in reversed(self._entries):
            if sid == signal_id:
                return (when, value)
        return None
