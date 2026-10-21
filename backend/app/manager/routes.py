"""Монтаж manager-режима: внутренний API, шлюз ролей, диспетчеризация.

Вызывается один раз из ``app/main.py`` в роли manager (ленивый импорт —
single-режим этот модуль не импортирует вовсе). Одна точка входа делает
три вещи:

1. внутренний API для станции (токен, постоянное время сравнения);
2. шлюз ролей — единая матрица из ``app.manager.roles``: оператору
   запрещены изменяющие эндпоинты, причём и на личной полосе, и на
   маршрутах дефолтного экземпляра этого же процесса;
3. диспетчеризация: запрос с опознанным пользователем уходит его личному
   экземпляру (прокси/WS), запрос без пользователя обслуживает сам
   процесс-менеджер — дефолтный экземпляр (:8000/:4840) как раньше.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from app.config import settings

from . import roles
from .instances import InstanceLimitError, InstanceRegistry, InstanceSpawnError
from .proxy import InstanceProxy

logger = logging.getLogger("opcua-imitator.manager")

#: Схема ответа /internal/instances — заморожена, станция строится по ней.
INSTANCES_SCHEMA = "imitator.instances.v1"

#: Запись дефолтного экземпляра в /internal/instances.
DEFAULT_USER = "default"
DEFAULT_HTTP_PORT = 8000

#: Момент подъёма менеджера — updated_at записи дефолтного экземпляра.
_MANAGER_BOOTED_AT = datetime.now(timezone.utc).isoformat()


def _default_opc_port() -> int:
    """Реальный OPC UA-порт дефолтного экземпляра — из его эндпоинта."""
    endpoint = os.getenv("OPC_UA_SERVER_ENDPOINT", "opc.tcp://0.0.0.0:4840")
    try:
        return int(endpoint.rsplit(":", 1)[-1])
    except (ValueError, IndexError):
        return 4840


def _default_entry() -> dict:
    return {
        "user": DEFAULT_USER,
        "http_port": DEFAULT_HTTP_PORT,
        "opc_port": _default_opc_port(),
        "state": "running",
        "endpoint": f"opc.tcp://{settings.IMITATOR_PUBLIC_OPC_HOST}:{_default_opc_port()}",
        # Дефолтная полоса остаётся анонимной (решение WP-F): станция ходит
        # на :4840 без имени, потому пара учётных данных пустая.
        "username": "",
        "password": "",
        "updated_at": _MANAGER_BOOTED_AT,
    }


def _token_ok(headers) -> bool:
    """Токен внутреннего API: сравнение в постоянном времени.

    Пустой IMITATOR_INTERNAL_TOKEN означает «API закрыт»: токена, которому
    можно доверить останов экземпляров и выдачу паролей, не задано.
    """
    expected = os.getenv("IMITATOR_INTERNAL_TOKEN", "")
    if not expected:
        return False
    got = headers.get("x-internal-token") or ""
    return hmac.compare_digest(got.encode(), expected.encode())


def _forbidden() -> JSONResponse:
    return JSONResponse(status_code=403, content={"code": "internal_forbidden"})


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ManagerDispatchMiddleware:
    """Шлюз ролей + раскладка запросов по экземплярам (единая точка)."""

    def __init__(self, app, registry: InstanceRegistry, proxy: InstanceProxy) -> None:
        self.app = app
        self._registry = registry
        self._proxy = proxy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # Внутренний API обслуживают маршруты: токен проверяется там же.
        if scope["type"] == "http" and path.startswith("/internal/"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        user, role = roles.resolve(headers)
        if user is None:
            # Дефолтная полоса: сам процесс-менеджер. Под тем же шлюзом —
            # записать в него оператор не может и без имени пользователя.
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
                return
            if roles.gate_rejection(role, scope.get("method", "GET"), path):
                await _send_json(send, 403, {"code": "role_forbidden"})
                return
            await self.app(scope, receive, send)
            return
        # Личная полоса: экземпляр пользователя. WebSocket — только чтение,
        # изменяющие HTTP-запросы оператора отбивает шлюз до прокси.
        if scope["type"] == "websocket":
            record = await self._ensure_record(send, user)
            if record is not None:
                await self._proxy.relay_ws(scope, receive, send, record)
            return
        if roles.gate_rejection(role, scope.get("method", "GET"), path):
            await _send_json(send, 403, {"code": "role_forbidden"})
            return
        record = await self._ensure_record(send, user)
        if record is None:
            return
        self._registry.touch(user)
        await self._proxy.relay_http(scope, receive, send, record)

    async def _ensure_record(self, send, user: str):
        try:
            return await self._registry.get_or_start(user)
        except InstanceLimitError:
            await _send_json(send, 503, {"code": "instance_limit"})
        except InstanceSpawnError as exc:
            logger.error("экземпляр %s не поднят: %s", user, exc)
            await _send_json(send, 502, {"code": "instance_unavailable"})
        return None


def install_manager(app, *, registry: Optional[InstanceRegistry] = None) -> InstanceRegistry:
    """Включить manager-режим на приложении (вызывается из app/main.py)."""
    if registry is None:
        registry = InstanceRegistry(
            users_root=Path(settings.IMITATOR_USERS_DATA_DIR),
            http_base_port=settings.IMITATOR_INSTANCE_HTTP_BASE_PORT,
            opc_base_port=settings.IMITATOR_INSTANCE_OPC_BASE_PORT,
            max_instances=settings.IMITATOR_MAX_INSTANCES,
            idle_timeout_s=settings.IMITATOR_IDLE_TIMEOUT_S,
            public_opc_host=settings.IMITATOR_PUBLIC_OPC_HOST,
        )
    proxy = InstanceProxy()
    router = APIRouter(prefix="/internal", tags=["manager"])

    @router.get("/instances")
    async def list_instances(request: Request):
        # Токен проверяется в обработчике, а не зависимостью: тело отказа
        # должно быть ровно {"code": "internal_forbidden"} без обёртки detail.
        if not _token_ok(request.headers):
            return _forbidden()
        instances = [_default_entry()]
        for record in sorted(registry.records(), key=lambda item: item.user):
            instances.append(record.public())
        return {"schema": INSTANCES_SCHEMA, "instances": instances}

    @router.post("/instances/{user}/stop")
    async def stop_instance(user: str, request: Request):
        if not _token_ok(request.headers):
            return _forbidden()
        stopped = await registry.stop(user)
        return {"status": "stopped" if stopped else "not_running", "user": user}

    app.include_router(router)
    app.add_middleware(ManagerDispatchMiddleware, registry=registry, proxy=proxy)

    async def _on_startup() -> None:
        registry.start_reaper()

    async def _on_shutdown() -> None:
        await registry.stop_reaper()
        await registry.stop_all()
        await proxy.aclose()
        await registry.aclose()

    # Хуки жизненного цикла: чистка простоя на старте, останов всех личных
    # экземпляров на shutting down (каждый сохранит свой снимок).
    app.router.on_startup.append(_on_startup)
    app.router.on_shutdown.append(_on_shutdown)
    return registry
