"""Heartbeat (cyclic refresh) режим встроенного OPC UA сервера.

Когда включён, одна фоновая задача на экземпляр приложения раз в секунду
переписывает текущие значения всех сигналов в адресное пространство, сохраняя
``Value`` и OPC UA тип, но присваивая новый ``SourceTimestamp`` (UTC) и
``StatusCode=Good`` — даже если значение не изменилось. Это имитирует
циклический опрос источника и даёт клиентам, которым важна свежесть данных,
наблюдать обновление по timestamp.

По умолчанию режим выключен; переключение — через ``set_enabled`` (API:
``GET/PUT /api/heartbeat``).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from app.services.embedded_server import EmbeddedOpcUaServer

logger = logging.getLogger("opcua-imitator.heartbeat")

# Fixed 1-second cycle per issue #1 (interval_ms stays a future knob).
DEFAULT_HEARTBEAT_INTERVAL = 1.0


class HeartbeatService:
    """Одна фоновая задача на экземпляр приложения; graceful cancellation."""

    def __init__(
        self,
        server: EmbeddedOpcUaServer,
        signal_ids: List[str],
        interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ):
        self._server = server
        self._signal_ids = list(signal_ids)
        self._interval = interval
        self._enabled = False
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        if enabled:
            self._enabled = True
            self._task = asyncio.create_task(self._run(), name="opcua-heartbeat")
            logger.info(
                "heartbeat enabled: %d signals, every %.2fs",
                len(self._signal_ids),
                self._interval,
            )
        else:
            self._enabled = False
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            logger.info("heartbeat disabled")

    async def _run(self) -> None:
        try:
            while self._enabled:
                when = datetime.now(timezone.utc)
                for signal_id in self._signal_ids:
                    await self._server.touch(signal_id, when)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("heartbeat task crashed; disabling")
            self._enabled = False
            self._task = None

    async def stop(self) -> None:
        await self.set_enabled(False)