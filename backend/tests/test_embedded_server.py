import asyncio
import socket

import pytest
from asyncua import Client, ua

from app.config import load_signal_config
from app.services.embedded_server import EmbeddedOpcUaServer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(coro) -> None:
    asyncio.run(coro)


def test_embedded_server_browse_and_write():
    async def scenario() -> None:
        port = _free_port()
        server = EmbeddedOpcUaServer(
            [
                {"id": "Application.GVL.Speed", "name": "Скорость", "type": "float", "writable": True, "default": 1.5},
                {"id": "Application.GVL.Alarm", "name": "Авария", "type": "bool", "writable": False, "default": False},
                {"id": "ProfibusData.MotorSpeed", "name": "Скорость двигателя", "type": "int", "writable": True, "default": 0},
            ],
            endpoint=f"opc.tcp://127.0.0.1:{port}",
        )
        await server.start()
        try:
            client = Client(f"opc.tcp://127.0.0.1:{port}")
            await client.connect()
            try:
                # переменные доступны по полному IEC-пути как строковым NodeId
                speed_node = server.nodes["Application.GVL.Speed"]
                assert await speed_node.read_value() == 1.5

                # запись через встроенную шину обновляет адресное пространство
                assert await server.write_value("Application.GVL.Speed", 42.0)
                assert await speed_node.read_value() == 42.0

                # запись внешним клиентом видна встроенному серверу
                ext_speed = client.get_node(speed_node.nodeid)
                await ext_speed.write_value(7.0)
                assert await server.read_value("Application.GVL.Speed") == 7.0

                # тип int сохраняется числом
                assert await server.write_value("ProfibusData.MotorSpeed", 1200)
                assert await server.read_value("ProfibusData.MotorSpeed") == 1200

                # только чтение: внешняя запись отклоняется
                ext_alarm = client.get_node(server.nodes["Application.GVL.Alarm"].nodeid)
                with pytest.raises(ua.UaStatusCodeError):
                    await ext_alarm.write_value(True)
            finally:
                await client.disconnect()
        finally:
            await server.stop()

    _run(scenario())


def test_embedded_server_input_bindings_config():
    async def scenario() -> None:
        port = _free_port()
        configs = load_signal_config()
        assert len(configs) == 101
        server = EmbeddedOpcUaServer(configs, endpoint=f"opc.tcp://127.0.0.1:{port}")
        await server.start()
        try:
            client = Client(f"opc.tcp://127.0.0.1:{port}")
            await client.connect()
            try:
                assert len(server.nodes) == 101
                assert server.namespace_index == 2

                sig_id = "ch1-10a3-sq1"
                node = server.nodes[sig_id]
                assert node.nodeid.to_string() == (
                    "ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal."
                    "x10A3DigitalInput.SQ1_SinhLeftTop"
                )
                assert await node.read_value() is False

                # типы из input-bindings сохраняются
                profibus = server.nodes["profibus-motor-speed"]
                assert await profibus.read_value() == 0

                # внешняя запись в входной сигнал отклоняется (read-only),
                # внутренняя — разрешена (симуляция)
                ext = client.get_node(node.nodeid)
                with pytest.raises(ua.UaStatusCodeError):
                    await ext.write_value(True)
                assert await server.write_value(sig_id, True)
                assert await ext.read_value() is True
            finally:
                await client.disconnect()
        finally:
            await server.stop()

    _run(scenario())