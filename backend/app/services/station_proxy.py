"""Прокси станции asutpv1_bigllm: read-only проход к её HTTP API.

Станция слушает loopback без CORS (и это её осознанное решение), поэтому
браузер стенда не может спрашивать её напрямую — проход идёт через
имитатор. Прокси только переводит байты: метод, путь, query и тело
уходят как есть, ответ — с исходным статусом и типом содержимого.
Белый список маршрутов держит прокси read-only: записать в станцию
через стенд нельзя в принципе, не потому что «пока запрещено».

Единственное содержательное исключение — снимок ``live_batch``: если
оператор не указал проект, подставляется проект станции по умолчанию
(``STATION_PROJECT_ID``). Недоступность станции — ``502
station_unreachable``, никогда не притворный пустой ответ.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("opcua-imitator.station")


class StationUnreachable(Exception):
    """Станция не отвечает: транспортная ошибка или таймаут."""


class StationProxy:
    """Один клиент на приложение к корню станции."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        timeout_s: Optional[float] = None,
    ) -> httpx.Response:
        """Один запрос; ``timeout_s`` перекрывает клиентский таймаут.

        Расчёт снапшота на станции — тяжёлый путь (исполнимая модель по всем
        выходам проекта) и в 5 секунд клиентского умолчания не укладывается,
        поэтому тяжёлые вызовы просят свой бюджет.
        """
        try:
            return await self._client.request(
                method, path, params=params, json=json, timeout=timeout_s
            )
        except httpx.TransportError as exc:
            raise StationUnreachable(f"{type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
