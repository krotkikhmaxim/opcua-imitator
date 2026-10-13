"""WS-фид дельт значений: раз в интервал — только изменившиеся сигналы.

Источник — шина (её адресное пространство, включая внешние записи через
OPC UA): фид читает все сигналы конфигурации, сравнивает с последней
отправкой и рассылает только изменившиеся. Заодно он следит за реестром
драйверов: смена активного драйвера уходит в WS как ``driver_changed``,
а шлюз heartbeat пересчитывается в каждом тике — так возвращение
heartbeat после сбоя маршрута не зависит от того, через какой код
остановился драйвер.

Интервал берётся из конфигурации; 0 отключает фид: клиент тогда опрашивает
REST сам. Класс ничего не знает о FastAPI — вещание приходит колбэком,
поэтому сервис тестируется без транспорта.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("opcua-imitator.values_feed")


class ValuesFeed:
    """Периодическая рассылка дельт значений и смен драйвера в WebSocket."""

    def __init__(
        self,
        reader: Callable[[str], Awaitable[Any]],
        signal_ids: List[str],
        broadcast: Callable[[dict], Awaitable[None]],
        interval_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        registry: Any = None,
        gate: Any = None,
    ) -> None:
        self._reader = reader
        self._signal_ids = signal_ids
        self._broadcast = broadcast
        self._interval_s = interval_s
        self._sleep = sleep
        self._now = now
        self._registry = registry
        self._gate = gate
        self._last: Dict[str, Any] = {}
        self._last_active: List[str] = []
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def disabled(self) -> bool:
        """Фид выключен конфигурацией (интервал 0)."""
        return self._interval_s <= 0

    def start(self) -> None:
        if self.disabled or self.running:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await self._sleep(self._interval_s)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("values feed tick failed")

    async def tick(self) -> Dict[str, Any]:
        """Один цикл сравнения; возвращает отправленные дельты (шов теста)."""
        changes: Dict[str, Any] = {}
        for signal_id in self._signal_ids:
            value = await self._reader(signal_id)
            if value is None:
                continue
            if signal_id not in self._last or self._last[signal_id] != value:
                self._last[signal_id] = value
                changes[signal_id] = value
        if changes:
            await self._broadcast({
                "type": "signals_update",
                "changes": changes,
                "at": self._now().isoformat(),
            })
        if self._registry is not None:
            active = self._registry.active()
            if active != self._last_active:
                self._last_active = active
                await self._broadcast({"type": "driver_changed", "active": active})
        if self._gate is not None:
            await self._gate.refresh()
        return changes
