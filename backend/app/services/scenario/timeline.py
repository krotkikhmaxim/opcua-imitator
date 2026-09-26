"""Временная шкала изменений сигналов с шагом 100 мс.

Сценарий описывается не состояниями, а изменениями: в журнал попадает только
смена значения (как у OPC UA подписки на изменение). Всё здесь чистое и
детерминированное — никакого времени, сети и случайности, кроме переданного
``random.Random``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Tuple

TICK_MS = 100


@dataclass(frozen=True)
class Change:
    """Одно изменение: сигнал получает значение в момент ``at_ms`` от начала цикла."""

    at_ms: int
    signal_id: str
    value: Any


class Timeline:
    """Изменения значений во времени; пишет только смену значения.

    Сигналы, которых нет в конфигурации имитатора, молча пропускаются:
    сценарий описывает установку целиком, а публикуется то, что опубликовано.
    Запись задним числом запрещена — иначе «текущее значение» шкалы перестало
    бы совпадать с тем, что увидит потребитель.
    """

    def __init__(self, current: Mapping[str, Any], known: Iterable[str]) -> None:
        self._current: Dict[str, Any] = dict(current)
        self._known = frozenset(known)
        self._changes: List[Change] = []
        self._last_at: Dict[str, int] = {}

    def value(self, signal_id: str) -> Any:
        return self._current.get(signal_id)

    def set(self, at_ms: int, signal_id: str, value: Any) -> None:
        if at_ms < 0 or at_ms % TICK_MS:
            raise ValueError(f"момент {at_ms} мс не кратен шагу {TICK_MS} мс")
        if at_ms < self._last_at.get(signal_id, 0):
            raise ValueError(f"запись {signal_id} задним числом: {at_ms} мс")
        self._last_at[signal_id] = at_ms
        if signal_id not in self._known:
            self._current[signal_id] = value
            return
        if signal_id in self._current and self._current[signal_id] == value:
            return
        self._current[signal_id] = value
        self._changes.append(Change(at_ms, signal_id, value))

    def set_bits(self, at_ms: int, signal_id: str, mask: int, on: bool) -> None:
        """Выставить или снять биты слова состояния (UInt16)."""
        word = int(self._current.get(signal_id) or 0)
        word = (word | mask) if on else (word & ~mask & 0xFFFF)
        self.set(at_ms, signal_id, word)

    def changes(self) -> Tuple[Change, ...]:
        # Сортировка устойчива: изменения одного момента идут в порядке записи.
        return tuple(sorted(self._changes, key=lambda change: change.at_ms))

    def state(self) -> Dict[str, Any]:
        return dict(self._current)


def wrap_int16(value: int) -> int:
    """Счётчик Int16 с переполнением, как аппаратный счётчик импульсов."""
    return ((value + 32768) % 65536) - 32768
