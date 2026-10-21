"""Прокси менеджера (WP-F): HTTP-проход и ретрансляция WebSocket.

HTTP: метод, путь, query, тело и заголовки проходят как есть (hop-by-hop и
Host — нет; не-ASCII значения отбрасываются — край присылает Remote-Name
любого алфавита, а httpx шлёт только ASCII), ответ дочернего экземпляра — со
своим статусом и заголовками.
WS: снимок и дельты идут от дочернего экземпляра дословно (seq не
перенумеровывается), клиентские тексты доходят до него.
"""

import asyncio
import json
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import MockTransport, Response
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from app.manager.instances import InstanceRecord
from app.manager.proxy import InstanceProxy

RECORD = InstanceRecord(
    user="ivan",
    slot=0,
    http_port=8101,
    opc_port=4841,
    username="imit_ivan",
    password="pw",
    created_at="2026-10-21T00:00:00+00:00",
    updated_at="2026-10-21T00:00:00+00:00",
)


class RelayApp:
    """Сырой ASGI: всё к прокси — как это делает middleware менеджера."""

    def __init__(self, proxy: InstanceProxy, record: InstanceRecord) -> None:
        self._proxy = proxy
        self._record = record

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "websocket":
            await self._proxy.relay_ws(scope, receive, send, self._record)
            return
        await self._proxy.relay_http(scope, receive, send, self._record)


def test_http_passthrough_method_path_query_body_headers():
    captured = {}

    def handler(request: httpx.Request) -> Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        captured["body"] = request.content
        captured["custom"] = request.headers.get("x-custom")
        captured["hop_by_hop"] = request.headers.get("te")
        captured["host"] = request.headers.get("host")
        return Response(
            201,
            content=json.dumps({"ok": True}).encode(),
            headers={"x-child-header": "child-value", "te": "trailers"},
        )

    proxy = InstanceProxy(client=MockTransport(handler))
    app = RelayApp(proxy, RECORD)

    with TestClient(app) as client:
        response = client.post(
            "/api/signals/write?op_id=abc",
            json={"id": "sig", "value": 5},
            headers={"x-custom": "1", "te": "trailers", "host": "opcuaimitator.ru"},
        )

    assert response.status_code == 201
    assert response.json() == {"ok": True}
    assert response.headers["x-child-header"] == "child-value"
    # hop-by-hop заголовок ответа не прошёл через прокси
    assert "te" not in response.headers

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/signals/write"
    assert captured["query"] == "op_id=abc"
    assert json.loads(captured["body"]) == {"id": "sig", "value": 5}
    assert captured["custom"] == "1"
    assert captured["hop_by_hop"] is None  # не переслан
    assert captured["host"] != "opcuaimitator.ru"  # Host переписан на дочерний

    asyncio.run(proxy.aclose())


def test_non_ascii_header_dropped_others_relayed():
    """Не-ASCII заголовок от края (Remote-Name с кириллицей) не роняет релей.

    httpx кодирует значения заголовков в ASCII; значение произвольного
    алфавита прежде падало с UnicodeEncodeError и запрос получал 500.
    Такие значения отбрасываются целиком (не искажаются), остальные доходят.
    """

    captured = {}

    def handler(request: httpx.Request) -> Response:
        captured["remote_name"] = request.headers.get("remote-name")
        captured["remote_user"] = request.headers.get("remote-user")
        captured["custom"] = request.headers.get("x-custom")
        return Response(200, content=b"{}")

    proxy = InstanceProxy(client=MockTransport(handler))
    app = RelayApp(proxy, RECORD)

    # Скоуп строится вручную: TestClient не даст отправить не-ASCII значение,
    # а именно его вставляет край (Caddy копирует Remote-Name от Authelia).
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "client": ("10.0.0.1", 12345),
        "server": ("opcuaimitator.ru", 443),
        "headers": [
            (b"remote-name", "Максим Кротких".encode("utf-8")),
            (b"remote-user", b"admin"),
            (b"x-custom", b"keep-me"),
        ],
    }
    messages = []
    body_received = b""

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))

    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    assert captured["remote_name"] is None  # не-ASCII отброшен, не исказён
    assert captured["remote_user"] == "admin"  # личность от края дошла
    assert captured["custom"] == "keep-me"

    asyncio.run(proxy.aclose())


def test_child_unavailable_is_502():
    def handler(request: httpx.Request) -> Response:
        raise httpx.ConnectError("упал", request=request)

    proxy = InstanceProxy(client=MockTransport(handler))
    app = RelayApp(proxy, RECORD)

    with TestClient(app) as client:
        response = client.get("/api/signals")

    assert response.status_code == 502
    assert response.json() == {"code": "instance_unavailable"}
    asyncio.run(proxy.aclose())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_ws_relay_snapshot_delta_and_client_text():
    """Снимок → дельта → текст клиента — всё доходит без перенумерации seq."""
    seen_from_client = []

    async def child_handler(connection):
        await connection.send(json.dumps({"type": "signals_snapshot", "seq": 7, "signals": {}}))
        message = await connection.recv()
        seen_from_client.append(message)
        await connection.send(json.dumps({"type": "signals_update", "seq": 8, "changes": {}}))
        # держим соединение, пока клиент не уйдёт
        try:
            async for _ in connection:
                pass
        except Exception:
            pass

    holder = {}

    async def start_server():
        server = await serve(child_handler, "127.0.0.1", 0)
        holder["server"] = server
        holder["port"] = server.sockets[0].getsockname()[1]
        return server

    record = replace(RECORD, http_port=0)
    proxy = InstanceProxy()
    app = RelayApp(proxy, record)

    with TestClient(app) as client:
        client.portal.start_task_soon(start_server).result()
        record.http_port = holder["port"]
        with client.websocket_connect("/ws") as ws:
            snapshot = json.loads(ws.receive_text())
            assert snapshot == {"type": "signals_snapshot", "seq": 7, "signals": {}}

            ws.send_text("ping-from-client")
            deadline = time.monotonic() + 5.0
            while not seen_from_client and time.monotonic() < deadline:
                time.sleep(0.05)
            assert seen_from_client == ["ping-from-client"]

            delta = json.loads(ws.receive_text())
            assert delta["seq"] == 8  # seq дочернего экземпляра не менялся
        holder["server"].close()

    asyncio.run(proxy.aclose())


def test_ws_child_disconnect_closes_client():
    """Обрыв дочерней стороны закрывает клиентскую (экземпляр остановлен)."""
    holder = {}

    async def child_handler(connection):
        await connection.send(json.dumps({"type": "signals_snapshot", "seq": 1}))
        await connection.close(code=1001)

    async def start_server():
        server = await serve(child_handler, "127.0.0.1", 0)
        holder["server"] = server
        holder["port"] = server.sockets[0].getsockname()[1]
        return server

    record = replace(RECORD)
    proxy = InstanceProxy()
    app = RelayApp(proxy, record)

    with TestClient(app) as client:
        client.portal.start_task_soon(start_server).result()
        record.http_port = holder["port"]
        with client.websocket_connect("/ws") as ws:
            first = json.loads(ws.receive_text())
            assert first["seq"] == 1
            # дочерний закрылся: прокси закрывает клиентскую сторону
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_text()
        holder["server"].close()

    asyncio.run(proxy.aclose())
