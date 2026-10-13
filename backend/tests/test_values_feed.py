"""WS-фид дельт значений: рассылка изменений, молчание без них, драйверы.

Фид — чистый сервис: читатель и вещание приходят колбэками, поэтому дельты
проверяются на фейковой шине без транспорта. Жизненный цикл (старт/стоп
вместе с приложением) — один тест на TestClient в режиме imitator.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

import pytest

from app.services.values_feed import ValuesFeed


class FakeBus:
    def __init__(self, values):
        self.values = dict(values)

    async def read_value(self, signal_id):
        return self.values.get(signal_id)


class FakeGate:
    def __init__(self):
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1


def _feed(values, interval=0.5, registry=None, gate=None):
    bus = FakeBus(values)
    sent = []
    feed = ValuesFeed(
        bus.read_value,
        list(values),
        lambda message: sent.append(message) or asyncio.sleep(0),
        interval,
        now=lambda: datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc),
        registry=registry,
        gate=gate,
    )
    return feed, bus, sent


def test_first_tick_sends_every_present_value():
    async def scenario():
        feed, bus, sent = _feed({"a": True, "b": 5})
        changes = await feed.tick()
        assert changes == {"a": True, "b": 5}
        assert sent[0]["type"] == "signals_update"
        assert sent[0]["changes"] == {"a": True, "b": 5}
        assert sent[0]["at"].startswith("2026-03-23T15:00:00")

    asyncio.run(scenario())


def test_unchanged_state_sends_nothing():
    async def scenario():
        feed, bus, sent = _feed({"a": True})
        await feed.tick()
        await feed.tick()
        await feed.tick()
        assert len(sent) == 1  # только бутстрап

    asyncio.run(scenario())


def test_only_the_changed_signal_is_sent():
    async def scenario():
        feed, bus, sent = _feed({"a": True, "b": 5})
        await feed.tick()
        bus.values["b"] = 7
        changes = await feed.tick()
        assert changes == {"b": 7}
        assert sent[-1]["changes"] == {"b": 7}

    asyncio.run(scenario())


def test_absent_values_are_skipped():
    async def scenario():
        feed, bus, sent = _feed({"a": True, "ghost": None})
        changes = await feed.tick()
        assert changes == {"a": True}
        bus.values["ghost"] = False  # появившийся сигнал уходит как дельта
        changes = await feed.tick()
        assert changes == {"ghost": False}

    asyncio.run(scenario())


def test_zero_interval_disables_the_feed():
    async def scenario():
        feed, _bus, _sent = _feed({"a": True}, interval=0)
        assert feed.disabled is True
        feed.start()
        assert feed.running is False  # старт при выключенном фиде — тишина

    asyncio.run(scenario())


class FakeRegistry:
    def __init__(self):
        self.active_drivers = []

    def active(self):
        return list(self.active_drivers)


def test_driver_change_is_broadcast_once_and_gate_refreshes_every_tick():
    async def scenario():
        registry = FakeRegistry()
        gate = FakeGate()
        feed, _bus, sent = _feed({"a": True}, registry=registry, gate=gate)

        await feed.tick()
        assert gate.refreshes == 1
        assert [m["type"] for m in sent] == ["signals_update"]

        registry.active_drivers = ["scenario"]
        await feed.tick()
        assert [m["type"] for m in sent] == ["signals_update", "driver_changed"]
        assert sent[-1]["active"] == ["scenario"]

        await feed.tick()
        # драйвер не сменился — повторной рассылки нет, шлюз тикает дальше
        assert [m["type"] for m in sent] == ["signals_update", "driver_changed"]
        assert gate.refreshes == 3

    asyncio.run(scenario())


def test_start_and_stop_follow_the_app_lifecycle():
    from fastapi.testclient import TestClient

    from app.main import _values_feed, app

    with TestClient(app):
        assert _values_feed.running is True
    assert _values_feed.running is False
