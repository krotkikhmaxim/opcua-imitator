"""Реестр драйверов значений: ручная запись / сценарий / плейбэк.

В каждый момент активен максимум один источник записей: ручная правка во
время сценария испортила бы журнал правды, сценарий во время плейбэка —
шкалу записи. Реестр — единственное место, которое об этом знает: HTTP-слой
спрашивает его перед ручной записью (``ensure_manual_writes_allowed``) и
перед запуском драйвера (``ensure_can_start``), а сам драйвер только
регистрирует предикат своей активности.

Здесь нет FastAPI: реестр бросает доменный ``DriverConflict``, перевод в
HTTP 409 — забота маршрутов, поэтому сервис остаётся тестируемым без
транспортного слоя.
"""

from __future__ import annotations

from typing import Callable, Dict, List


class DriverConflict(Exception):
    """Активен другой драйвер; машинный код — для HTTP-ответа."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ValueDriverRegistry:
    """Реестр «кто сейчас пишет в шину» с проверками взаимного исключения."""

    def __init__(self) -> None:
        self._drivers: Dict[str, Callable[[], bool]] = {}

    def register(self, driver_id: str, is_active: Callable[[], bool]) -> None:
        self._drivers[driver_id] = is_active

    def unregister(self, driver_id: str) -> None:
        self._drivers.pop(driver_id, None)

    def active(self) -> List[str]:
        return [driver_id for driver_id, check in self._drivers.items() if check()]

    def any_active(self) -> bool:
        return any(check() for check in self._drivers.values())

    def ensure_manual_writes_allowed(self) -> None:
        """Ручная запись допустима только при пустом реестре."""
        for driver_id, check in self._drivers.items():
            if check():
                raise DriverConflict(
                    "driver_active",
                    f"Активен драйвер «{driver_id}»: ручная запись отключена, "
                    "остановите его",
                )

    def ensure_can_start(self, driver_id: str) -> None:
        """Запуск драйвера допустим, если не активен никакой другой."""
        for other_id, check in self._drivers.items():
            if other_id != driver_id and check():
                raise DriverConflict(
                    "driver_active",
                    f"Активен драйвер «{other_id}»: сначала остановите его",
                )
