"""Шлюз heartbeat: на время активного драйвера глушит циклическую перезапись.

``make run`` включает heartbeat сам — значения обновляются, но каждая
перезапись ставит свежий SourceTimestamp и ломает шкалу сценария или
плейбэка для потребителя (коллектор станции видит «данные не стареют»
вместо точных интервалов события). Шлюз запоминает, был ли heartbeat включён
до появления драйвера, выключает его на время активности и возвращает после.

``refresh`` идемпотентен и дёшев: его дёргают HTTP-ручки запуска/остановки
драйверов и периодический фид значений, поэтому драйвер, закончившийся сам
(``once=True``), тоже освобождает heartbeat без участия HTTP.
"""

from __future__ import annotations

from typing import Callable, Optional

from app.services.drivers.registry import ValueDriverRegistry


class HeartbeatGate:
    def __init__(
        self,
        registry: ValueDriverRegistry,
        get_heartbeat: Callable[[], Optional[object]],
    ) -> None:
        self._registry = registry
        self._get_heartbeat = get_heartbeat
        # None — шлюз вне эпизода драйвера; иначе — что было до него.
        self._restore_to: Optional[bool] = None

    async def refresh(self) -> None:
        heartbeat = self._get_heartbeat()
        if heartbeat is None:
            return
        busy = self._registry.any_active()
        if busy:
            if self._restore_to is None:
                self._restore_to = bool(heartbeat.enabled)
            if heartbeat.enabled:
                await heartbeat.set_enabled(False)
            return
        if self._restore_to is not None:
            restore, self._restore_to = self._restore_to, None
            if restore and not heartbeat.enabled:
                await heartbeat.set_enabled(True)

    @property
    def holding(self) -> bool:
        """Шлюз сейчас держит heartbeat выключенным из-за драйвера."""
        return self._restore_to is not None
