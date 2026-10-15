"""Владение сигналами (ТЗ #5, шаг 5 — §9.2).

Сценарий до запуска объявляет набор изменяемых сигналов и получает
исключительное владение им: ручная, пакетная и любая другая внутренняя
запись в занятый сигнал отклоняется, пока владение не снято. Непересекающиеся
прогоны работают одновременно; пересекающийся запуск отклоняется целиком
с перечнем конфликтов. Пауза и ошибка сохраняют владение (ручная запись
не должна менять условия продолжения); завершение и остановка освобождают.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Owner:
    """Кто держит сигнал: машина-читаемое имя и человеческий титул."""

    id: str  # например "scenario:<run_id>" — им же подписаны записи источника
    kind: str  # "scenario"
    title: str  # для оператора: «Режим „Аварийный стоп“ (run ab12cd)»


@dataclass(frozen=True)
class OwnershipConflict:
    signal_id: str
    owner: Owner


class OwnershipRegistry:
    """Сигнал → владелец. Приобретение — всё или ничего."""

    def __init__(self) -> None:
        self._held: Dict[str, Owner] = {}
        # Монотонный номер состояния владения: фид WS рассылает карту
        # владельцев, только когда он вырос (ТЗ #5 §12).
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def acquire(self, owner: Owner, signals: Iterable[str]) -> List[OwnershipConflict]:
        """Заявить набор; при любом конфликте ничего не занято, список возвращён."""
        wanted = list(dict.fromkeys(signals))
        conflicts = [
            OwnershipConflict(signal_id=signal_id, owner=self._held[signal_id])
            for signal_id in wanted
            if signal_id in self._held and self._held[signal_id].id != owner.id
        ]
        if conflicts:
            return conflicts
        for signal_id in wanted:
            self._held[signal_id] = owner
        self._version += 1
        return []

    def release(self, owner_id: str) -> List[str]:
        """Снять все владения источника; вернуть освобождённые сигналы."""
        freed = [sid for sid, held in self._held.items() if held.id == owner_id]
        for sid in freed:
            del self._held[sid]
        if freed:
            self._version += 1
        return freed

    def owner_of(self, signal_id: str) -> Optional[Owner]:
        return self._held.get(signal_id)

    def held_signals(self) -> Dict[str, Owner]:
        return dict(self._held)

    def active_owners(self) -> List[Owner]:
        seen: Dict[str, Owner] = {}
        for owner in self._held.values():
            seen.setdefault(owner.id, owner)
        return list(seen.values())
