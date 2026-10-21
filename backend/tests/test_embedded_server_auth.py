"""Учётные данные встроенного OPC UA сервера (manager-режим, WP-F).

Когда пара OPC_UA_SERVER_USER/OPC_UA_SERVER_PASSWORD задана (менеджер
inject'ит её личным экземплярам), анонимные сессии отклоняются, а названная
пара принимается. Без пары — анонимный доступ, как прежде (дефолтная
полоса :4840 и все прежние тесты не меняются).
"""

import asyncio
import socket

import pytest
from asyncua import Client, ua

from app.services.embedded_server import EmbeddedOpcUaServer

CONFIGS = [
    {"id": "Application.GVL.Speed", "name": "Скорость", "type": "float", "default": 1.5},
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(coro) -> None:
    asyncio.run(coro)


def test_anonymous_rejected_when_credentials_set():
    """Анонимная активация сессии отклоняется при заданной паре логин/пароль."""

    async def scenario() -> None:
        endpoint = f"opc.tcp://127.0.0.1:{_free_port()}"
        server = EmbeddedOpcUaServer(
            CONFIGS, endpoint=endpoint, username="imit_ivan", password="s3cret-pw"
        )
        await server.start()
        try:
            client = Client(endpoint)
            with pytest.raises(ua.uaerrors.BadUserAccessDenied):
                await client.connect()
        finally:
            await server.stop()

    _run(scenario())


def test_password_auth_accepted_and_wrong_password_rejected():
    """Названная пара проходит активацию, чужая — нет; чтение узла работает."""

    async def scenario() -> None:
        endpoint = f"opc.tcp://127.0.0.1:{_free_port()}"
        server = EmbeddedOpcUaServer(
            CONFIGS, endpoint=endpoint, username="imit_ivan", password="s3cret-pw"
        )
        await server.start()
        try:
            good = Client(endpoint)
            good.set_user("imit_ivan")
            good.set_password("s3cret-pw")
            await good.connect()
            try:
                value = await good.get_node(
                    server.nodes["Application.GVL.Speed"].nodeid
                ).read_value()
                assert value == 1.5
            finally:
                await good.disconnect()

            bad = Client(endpoint)
            bad.set_user("imit_ivan")
            bad.set_password("wrong")
            with pytest.raises(ua.uaerrors.BadUserAccessDenied):
                await bad.connect()
        finally:
            await server.stop()

    _run(scenario())


def test_anonymous_allowed_when_credentials_unset():
    """Пара не задана — анонимная сессия работает (прежнее поведение)."""

    async def scenario() -> None:
        endpoint = f"opc.tcp://127.0.0.1:{_free_port()}"
        server = EmbeddedOpcUaServer(CONFIGS, endpoint=endpoint)
        await server.start()
        try:
            client = Client(endpoint)
            await client.connect()
            try:
                value = await client.get_node(
                    server.nodes["Application.GVL.Speed"].nodeid
                ).read_value()
                assert value == 1.5
            finally:
                await client.disconnect()
        finally:
            await server.stop()

    _run(scenario())
