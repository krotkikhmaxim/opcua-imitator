"""Обратный прокси менеджера: HTTP-проход и ретрансляция WebSocket.

Запросы к личному экземпляру пересылаются как есть — метод, путь, query,
тело, заголовки (кроме hop-by-hop и Host); фронтенд отдаётся дочерним
процессом дословно, никаких изменений на стороне менеджера. WebSocket
``/ws`` ретранслируется в обе стороны: снимок и дельты идут от дочернего
процесса, seq у каждого экземпляра свой — менеджер ничего не
перенумеровывает и не кэширует.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .instances import InstanceRecord

logger = logging.getLogger("opcua-imitator.manager.proxy")

# Hop-by-hop заголовки (RFC 7230 §6.1): их смысл — соединение конкретного
# сегмента, через границу прокси они не проходят.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _request_headers(scope: dict) -> List[Tuple[str, str]]:
    headers = []
    for raw_name, raw_value in scope.get("headers") or ():
        name = raw_name.decode("latin-1")
        if name.lower() in _HOP_BY_HOP or name.lower() == "host":
            continue
        try:
            headers.append((name, raw_value.decode("ascii")))
        except UnicodeDecodeError:
            # Значение не ASCII (край пересылает Remote-Name — display name
            # произвольного алфавита): httpx при отправке упадёт с
            # UnicodeEncodeError и запрос получит 500. Ребёнок читает только
            # Remote-User/Remote-Groups — они ASCII по построению; остальное
            # честно отбрасывается, а не передаётся искажённым.
            logger.warning("прокси: заголовок %s отброшен (значение не ASCII)", name)
    return headers


def _upstream_url(scope: dict, record: InstanceRecord) -> str:
    path = scope.get("path", "/")
    query = scope.get("query_string") or b""
    suffix = f"?{query.decode('latin-1')}" if query else ""
    return f"http://127.0.0.1:{record.http_port}{path}{suffix}"


def _ws_url(scope: dict, record: InstanceRecord) -> str:
    path = scope.get("path", "/ws")
    query = scope.get("query_string") or b""
    suffix = f"?{query.decode('latin-1')}" if query else ""
    return f"ws://127.0.0.1:{record.http_port}{path}{suffix}"


class InstanceProxy:
    """HTTP/WebSocket-проход к дочернему экземпляру по его слоту."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        # Для тестов сюда можно передать и транспорт (httpx.MockTransport):
        # оборачиваем его в клиента.
        if client is not None and not isinstance(client, httpx.AsyncClient):
            client = httpx.AsyncClient(transport=client)
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(None))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- HTTP ----------------------------------------------------------------

    async def relay_http(self, scope: dict, receive, send, record: InstanceRecord) -> None:
        """Запрос → дочерний процесс, ответ → клиенту (тело целиком)."""
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            elif message["type"] == "http.disconnect":
                break
        try:
            response = await self._http().request(
                scope.get("method", "GET"),
                _upstream_url(scope, record),
                content=body if body else None,
                headers=_request_headers(scope),
            )
        except httpx.HTTPError:
            logger.exception("прокси: дочерний экземпляр %s не отвечает", record.user)
            await _send_json(send, 502, {"code": "instance_unavailable"})
            return
        raw_headers = [
            (name, value)
            for name, value in response.headers.raw
            if name.decode("latin-1").lower() not in _HOP_BY_HOP
        ]
        content = response.content
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": raw_headers,
            }
        )
        await send({"type": "http.response.body", "body": content})
        await response.aclose()

    # -- WebSocket -------------------------------------------------------------

    async def relay_ws(self, scope: dict, receive, send, record: InstanceRecord) -> None:
        """Обе стороны качают свои направления; чей-то обрыв закрывает вторую."""
        async with ws_connect(_ws_url(scope, record)) as upstream:
            await send({"type": "websocket.accept"})
            upstream_closed = asyncio.Event()

            async def client_to_upstream() -> None:
                while True:
                    message = await receive()
                    if message["type"] == "websocket.receive":
                        if "text" in message and message["text"] is not None:
                            await upstream.send(message["text"])
                        elif "bytes" in message and message["bytes"] is not None:
                            await upstream.send(message["bytes"])
                    elif message["type"] == "websocket.disconnect":
                        await _close_quietly(upstream)
                        upstream_closed.set()
                        return

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await send({"type": "websocket.send", "bytes": message})
                        else:
                            await send({"type": "websocket.send", "text": message})
                except ConnectionClosed:
                    pass
                finally:
                    upstream_closed.set()
                    await _close_downstream(send)

            tasks = [
                asyncio.ensure_future(client_to_upstream()),
                asyncio.ensure_future(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending)
            for task in done:
                try:
                    exc = task.exception()
                except asyncio.CancelledError:
                    continue
                if exc is not None:
                    # Обрыв стороны клиента/дочернего процесса — штатный финал
                    # ретрансляции, а не ошибка менеджера.
                    logger.debug("ws relay %s завершён: %r", record.user, exc)
            if not upstream_closed.is_set():
                await _close_quietly(upstream)


async def _close_quietly(upstream) -> None:
    try:
        await upstream.close(code=1001)
    except Exception:
        pass


async def _close_downstream(send) -> None:
    try:
        await send({"type": "websocket.close", "code": 1001})
    except Exception:
        pass


async def _send_json(send, status: int, payload: dict) -> None:
    import json

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
