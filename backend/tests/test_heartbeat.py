"""Тесты heartbeat-режима (cyclic refresh SourceTimestamp), issue #1.

Покрывают критерии приёмки:
- по умолчанию heartbeat выключен;
- после включения каждое актуальное значение переписывается циклически,
  значение и OPC UA тип сохраняются, а SourceTimestamp монотонно растёт;
- после выключения timestamp для неизменившихся значений перестаёт
  обновляться;
- API чтения/переключения режима (вне режима server — недоступен);
- lifecycle: одна задача, graceful cancellation.
"""

import asyncio
import datetime
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# API-тесты должны импортировать app.main в режиме имитатора, чтобы не
# поднимать встроенный OPC UA сервер и не ломать соседние тесты (test_states).
os.environ["OPC_UA_MODE"] = "imitator"

import pytest
from asyncua import Client, ua
from fastapi.testclient import TestClient

from app.main import app
from app.services.embedded_server import EmbeddedOpcUaServer
from app.services.heartbeat import HeartbeatService


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(coro) -> None:
    asyncio.run(coro)


def _signals():
    return [
        {"id": "A.Speed", "name": "Скорость", "type": "float", "writable": True, "default": 1.5},
        {"id": "A.Alarm", "name": "Авария", "type": "bool", "writable": True, "default": False},
        {"id": "P.MotorSpeed", "name": "Скорость двигателя", "type": "Int16", "writable": True, "default": 0},
    ]


def _old_ts() -> datetime.datetime:
    return datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


async def _write_with_ts(node, value, vtype, when):
    await node.write_value(
        ua.DataValue(ua.Variant(value, vtype), ua.StatusCode(ua.StatusCodes.Good), when)
    )


def test_heartbeat_disabled_by_default():
    async def scenario() -> None:
        server = EmbeddedOpcUaServer(_signals(), endpoint=f"opc.tcp://127.0.0.1:{_free_port()}")
        await server.start()
        try:
            hb = HeartbeatService(server, [c["id"] for c in _signals()])
            assert hb.enabled is False
            # выключенный режим не трогает timestamp: записали старое время,
            # подождали — оно не изменилось
            node = server.nodes["A.Speed"]
            await _write_with_ts(node, 42.0, ua.VariantType.Double, _old_ts())
            await asyncio.sleep(0.2)
            assert (await node.read_data_value()).SourceTimestamp == _old_ts()
        finally:
            await server.stop()

    _run(scenario())


def test_heartbeat_cycles_source_timestamp_and_preserves_value():
    async def scenario() -> None:
        port = _free_port()
        server = EmbeddedOpcUaServer(_signals(), endpoint=f"opc.tcp://127.0.0.1:{port}")
        await server.start()
        hb = HeartbeatService(server, [c["id"] for c in _signals()], interval=0.05)
        try:
            node = server.nodes["A.Speed"]
            await _write_with_ts(node, 42.0, ua.VariantType.Double, _old_ts())

            await hb.set_enabled(True)
            assert hb.enabled is True

            await asyncio.sleep(0.25)  # несколько циклов

            after = await node.read_data_value()
            assert after.Value.Value == 42.0  # значение сохранено
            assert after.Value.VariantType == ua.VariantType.Double  # тип сохранён
            assert after.SourceTimestamp is not None
            assert after.SourceTimestamp > _old_ts()  # timestamp обновлён
            assert after.StatusCode.value == 0  # Good

            # цикл касается каждого сигнала, включая Boolean
            alarm = await server.nodes["A.Alarm"].read_data_value()
            assert alarm.SourceTimestamp is not None
            assert alarm.SourceTimestamp > _old_ts()
        finally:
            await hb.stop()
            await server.stop()

    _run(scenario())


def test_heartbeat_preserves_int_type():
    async def scenario() -> None:
        port = _free_port()
        server = EmbeddedOpcUaServer(_signals(), endpoint=f"opc.tcp://127.0.0.1:{port}")
        await server.start()
        hb = HeartbeatService(server, [c["id"] for c in _signals()], interval=0.05)
        try:
            node = server.nodes["P.MotorSpeed"]  # Int16
            await _write_with_ts(node, 1200, ua.VariantType.Int16, _old_ts())
            await hb.set_enabled(True)
            await asyncio.sleep(0.2)
            after = await node.read_data_value()
            assert after.Value.Value == 1200
            assert after.Value.VariantType == ua.VariantType.Int16
        finally:
            await hb.stop()
            await server.stop()

    _run(scenario())


def test_heartbeat_disabled_stops_updates():
    async def scenario() -> None:
        port = _free_port()
        server = EmbeddedOpcUaServer(_signals(), endpoint=f"opc.tcp://127.0.0.1:{port}")
        await server.start()
        hb = HeartbeatService(server, [c["id"] for c in _signals()], interval=0.05)
        try:
            node = server.nodes["A.Speed"]
            await _write_with_ts(node, 42.0, ua.VariantType.Double, _old_ts())

            await hb.set_enabled(True)
            await asyncio.sleep(0.2)
            frozen = (await node.read_data_value()).SourceTimestamp
            assert frozen > _old_ts()

            await hb.set_enabled(False)
            assert hb.enabled is False
            frozen = (await node.read_data_value()).SourceTimestamp

            await asyncio.sleep(0.25)  # несколько интервалов без heartbeat
            after = await node.read_data_value()
            assert after.SourceTimestamp == frozen  # timestamp заморожен
            assert after.Value.Value == 42.0
        finally:
            await hb.stop()
            await server.stop()

    _run(scenario())


def test_heartbeat_visible_to_external_client():
    """Внешний OPC UA клиент видит обновление SourceTimestamp без смены value."""

    async def scenario() -> None:
        port = _free_port()
        server = EmbeddedOpcUaServer(_signals(), endpoint=f"opc.tcp://127.0.0.1:{port}")
        await server.start()
        hb = HeartbeatService(server, [c["id"] for c in _signals()], interval=0.05)
        client = Client(f"opc.tcp://127.0.0.1:{port}")
        await client.connect()
        try:
            ext_node = client.get_node(server.nodes["A.Speed"].nodeid)
            await _write_with_ts(server.nodes["A.Speed"], 7.0, ua.VariantType.Double, _old_ts())

            before = await ext_node.read_data_value()
            assert before.Value.Value == 7.0

            await hb.set_enabled(True)
            await asyncio.sleep(0.25)

            after = await ext_node.read_data_value()
            assert after.Value.Value == 7.0  # значение не изменилось
            assert after.SourceTimestamp > before.SourceTimestamp  # свежесть видна
        finally:
            await client.disconnect()
            await hb.stop()
            await server.stop()

    _run(scenario())


def test_heartbeat_stop_graceful():
    async def scenario() -> None:
        server = EmbeddedOpcUaServer(_signals(), endpoint=f"opc.tcp://127.0.0.1:{_free_port()}")
        await server.start()
        try:
            hb = HeartbeatService(server, [c["id"] for c in _signals()], interval=0.01)
            await hb.set_enabled(True)
            await hb.stop()
            assert hb.enabled is False
            # повторный stop и повторное включение не падают
            await hb.stop()
            await hb.set_enabled(True)
            assert hb.enabled is True
            await hb.stop()
        finally:
            await server.stop()

    _run(scenario())


# --- API -------------------------------------------------------------------

def test_heartbeat_api_unsupported_outside_server_mode():
    """В режиме имитатора heartbeat недоступен: GET сообщает об этом, PUT — 409."""
    with TestClient(app) as client:
        r = client.get("/api/heartbeat")
        assert r.status_code == 200
        assert r.json() == {"enabled": False, "supported": False}

        r = client.put("/api/heartbeat", json={"enabled": True})
        assert r.status_code == 409


def test_heartbeat_api_rejects_bad_body():
    with TestClient(app) as client:
        # отсутствие обязательного поля enabled — 422 (pydantic), а не 409
        r = client.put("/api/heartbeat", json={})
        assert r.status_code == 422
        # некоэрсируемое в bool значение — тоже 422
        r = client.put("/api/heartbeat", json={"enabled": []})
        assert r.status_code == 422