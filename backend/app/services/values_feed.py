"""WS-фид дельт: раз в интервал — изменившиеся сигналы с ревизиями (ТЗ #5 §12).

Источник — шина (её адресное пространство, включая внешние записи через
OPC UA): фид читает все сигналы конфигурации, сравнивает с последней
отправкой и рассылает изменившиеся с карточкой ``{v, r, s}`` — значение,
ревизия, источник последнего изменения. Сообщение уходит КАЖДЫЙ тик с
монотонным ``seq``: клиент по пропуску в ``seq`` замечает потерянные
сообщения и перезапрашивает снимок (пустой тик — это тоже событие
«изменений нет», а не тишина, неотличимая от обрыва).

Карта владельцев уходит вместе с дельтой, только когда вырос
``ownership.version`` — держать её в каждом тике незачем.

Заодно фид следит за реестром драйверов: смена активного драйвера уходит
в WS как ``driver_changed``, а шлюз heartbeat пересчитывается в каждом
тике — так возвращение heartbeat после сбоя маршрута не зависит от того,
через какой код остановился драйвер.

Интервал берётся из конфигурации; 0 отключает фид: клиент тогда опрашивает
REST сам. Класс ничего не знает о FastAPI — вещание приходит колбэком,
поэтому сервис тестируется без транспорта.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("opcua-imitator.values_feed")


class ValuesFeed:
    """Периодическая рассылка дельт значений, владельцев и смен драйвера."""

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
        revision_of: Optional[Callable[[str], int]] = None,
        source_of: Optional[Callable[[str], Optional[str]]] = None,
        ownership: Any = None,
    ) -> None:
        self._reader = reader
        self._signal_ids = signal_ids
        self._broadcast = broadcast
        self._interval_s = interval_s
        self._sleep = sleep
        self._now = now
        self._registry = registry
        self._gate = gate
        self._revision_of = revision_of or (lambda _sid: 0)
        self._source_of = source_of or (lambda _sid: None)
        self._ownership = ownership
        self._last: Dict[str, Any] = {}
        self._last_active: List[str] = []
        self._last_owner_version: Optional[int] = None
        self._last_owners: Dict[str, Optional[str]] = {}
        self._seq = 0
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def disabled(self) -> bool:
        """Фид выключен конфигурацией (интервал 0)."""
        return self._interval_s <= 0

    @property
    def seq(self) -> int:
        """Номер последнего тика: снимок WS метится им же (ТЗ #5 §12)."""
        return self._seq

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

    def _ownership_delta(self) -> Dict[str, Optional[Dict[str, str]]]:
        """Изменения карты владельцев с прошлого тика (пусто без смены версии)."""
        if self._ownership is None:
            return {}
        version = self._ownership.version
        if self._last_owner_version is None:
            # Первый тик: состояние фиксируется без рассылки — новым клиентам
            # владельцев отдаёт снимок при подключении.
            self._last_owner_version = version
            self._last_owners = {
                sid: (
                    holder.id
                    if (holder := self._ownership.owner_of(sid)) is not None
                    else None
                )
                for sid in self._signal_ids
            }
            return {}
        if version == self._last_owner_version:
            return {}
        self._last_owner_version = version
        delta: Dict[str, Optional[Dict[str, str]]] = {}
        for sid in self._signal_ids:
            holder = self._ownership.owner_of(sid)
            owner_id = holder.id if holder is not None else None
            if owner_id != self._last_owners.get(sid):
                self._last_owners[sid] = owner_id
                delta[sid] = (
                    {"id": holder.id, "title": holder.title} if holder else None
                )
        return delta

    async def tick(self) -> Dict[str, Any]:
        """Один цикл сравнения; возвращает отправленные дельты (шов теста)."""
        self._seq += 1
        changes: Dict[str, Any] = {}
        for signal_id in self._signal_ids:
            value = await self._reader(signal_id)
            if value is None:
                continue
            if signal_id not in self._last or self._last[signal_id] != value:
                self._last[signal_id] = value
                changes[signal_id] = {
                    "v": value,
                    "r": self._revision_of(signal_id),
                    "s": self._source_of(signal_id),
                }
        message: Dict[str, Any] = {
            "type": "signals_update",
            "seq": self._seq,
            "changes": changes,
            "at": self._now().isoformat(),
        }
        owners = self._ownership_delta()
        if owners:
            message["owners"] = owners
        await self._broadcast(message)
        if self._registry is not None:
            active = self._registry.active()
            if active != self._last_active:
                self._last_active = active
                await self._broadcast({"type": "driver_changed", "active": active})
        if self._gate is not None:
            await self._gate.refresh()
        return changes

    async def read_card(self, signal_id: str) -> Tuple[Any, int, Optional[str]]:
        """Значение с ревизией и источником — для снимка при подключении WS."""
        return (
            await self._reader(signal_id),
            self._revision_of(signal_id),
            self._source_of(signal_id),
        )
