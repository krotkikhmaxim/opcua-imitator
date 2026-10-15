"""WS-фид дельт: ревизии в дельтах, seq на каждом тике, карта владельцев.

Фид — чистый сервис: читатель, ревизии, источники и вещание приходят
колбэками, поэтому дельты проверяются на фейковой шине без транспорта.
Контракт §12: сообщение каждый тик с монотонным seq (пропуск замечается
клиентом), дельта несёт {v, r, s}, владельцы — только при смене версии.
Жизненный цикл (старт/стоп вместе с приложением) и снимок при подключении
WS — тесты на TestClient в режиме imitator.
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


class FakeOwnership:
    def __init__(self, version=0, held=None):
        self.version = version
        self.held = dict(held or {})

    def owner_of(self, signal_id):
        return self.held.get(signal_id)


def _feed(values, interval=0.5, registry=None, gate=None, ownership=None):
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
        revision_of=lambda sid: {"a": 3, "b": 1}.get(sid, 0),
        source_of=lambda sid: {"a": "manual"}.get(sid),
        ownership=ownership,
    )
    return feed, bus, sent


def test_first_tick_sends_every_present_value_with_card():
    async def scenario():
        feed, bus, sent = _feed({"a": True, "b": 5})
        changes = await feed.tick()
        assert changes == {
            "a": {"v": True, "r": 3, "s": "manual"},
            "b": {"v": 5, "r": 1, "s": None},
        }
        assert sent[0]["type"] == "signals_update"
        assert sent[0]["seq"] == 1
        assert sent[0]["changes"] == changes
        assert sent[0]["at"].startswith("2026-03-23T15:00:00")

    asyncio.run(scenario())


def test_every_tick_carries_seq_even_without_changes():
    async def scenario():
        feed, _bus, sent = _feed({"a": True})
        await feed.tick()
        await feed.tick()
        # Пустой тик — тоже событие: клиент отличает «изменений нет» от обрыва.
        assert [m["seq"] for m in sent] == [1, 2]
        assert sent[1]["changes"] == {}

    asyncio.run(scenario())


def test_only_the_changed_signal_is_sent():
    async def scenario():
        feed, bus, sent = _feed({"a": True, "b": 5})
        await feed.tick()
        bus.values["b"] = 7
        changes = await feed.tick()
        assert changes == {"b": {"v": 7, "r": 1, "s": None}}
        assert sent[-1]["changes"] == {"b": {"v": 7, "r": 1, "s": None}}

    asyncio.run(scenario())


def test_absent_values_are_skipped():
    async def scenario():
        feed, bus, sent = _feed({"a": True, "ghost": None})
        changes = await feed.tick()
        assert changes == {"a": {"v": True, "r": 3, "s": "manual"}}
        bus.values["ghost"] = False  # появившийся сигнал уходит как дельта
        changes = await feed.tick()
        assert changes == {"ghost": {"v": False, "r": 0, "s": None}}

    asyncio.run(scenario())


def test_zero_interval_disables_the_feed():
    async def scenario():
        feed, _bus, _sent = _feed({"a": True}, interval=0)
        assert feed.disabled is True
        feed.start()
        assert feed.running is False  # старт при выключенном фиде — тишина

    asyncio.run(scenario())


def test_owner_map_is_sent_only_when_version_grows():
    async def scenario():
        from types import SimpleNamespace

        owner = SimpleNamespace(id="scenario:r1", title="Режим «Тест»")
        own = FakeOwnership(version=0)
        feed, _bus, sent = _feed({"a": True}, ownership=own)

        await feed.tick()
        # Первый тик только фиксирует состояние: рассылки карты нет.
        assert "owners" not in sent[0]

        own.version = 1
        own.held["a"] = owner
        await feed.tick()
        assert sent[1]["owners"] == {"a": {"id": "scenario:r1", "title": "Режим «Тест»"}}

        await feed.tick()
        assert "owners" not in sent[2]  # версия не менялась — повтора нет

        own.version = 2
        own.held.clear()
        await feed.tick()
        assert sent[3]["owners"] == {"a": None}  # освобождение доходит как None

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
        # signals_update уходит каждый тик; driver_changed — только на смену.
        assert [m["type"] for m in sent].count("driver_changed") == 1
        assert sent[-1]["active"] == ["scenario"]

        await feed.tick()
        # драйвер не сменился — повторной рассылки нет, шлюз тикает дальше
        assert [m["type"] for m in sent].count("driver_changed") == 1
        assert sent[-1]["type"] == "signals_update"
        assert gate.refreshes == 3

    asyncio.run(scenario())


def test_start_and_stop_follow_the_app_lifecycle():
    from fastapi.testclient import TestClient

    from app.main import _values_feed, app

    with TestClient(app):
        assert _values_feed.running is True
    assert _values_feed.running is False


def test_ws_connect_receives_snapshot_then_contiguous_seq():
    """§12: подключение начинается со снимка; первая дельта продолжает seq.

    Тик между снимком и подпиской был бы пропущен — и замечен клиентом как
    скачок seq. Здесь смежность и есть норма.
    """
    from fastapi.testclient import TestClient

    from app.main import _cache, _values_feed, app

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "signals_snapshot"
            assert len(snapshot["signals"]) == len(_cache.config)
            card = snapshot["signals"]["ch1-10a3-sq1"]
            assert card["revision"] >= 0
            assert "source" in card and "owner_id" in card

            # Тик на цикле приложения: дельта доходит через реальный сокет
            # (авто-тик фида, поднятого startup'ом, дал бы тот же seq).
            client.portal.start_task_soon(_values_feed.tick)
            delta = ws.receive_json()
            assert delta["type"] == "signals_update"
            assert delta["seq"] == snapshot["seq"] + 1


def test_two_clients_receive_identical_deltas():
    """§16 Multi-client: несколько UI получают одни и те же подтверждённые дельты."""
    from fastapi.testclient import TestClient

    from app.main import _cache, _values_feed, app

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as first, client.websocket_connect("/ws") as second:
            snap_a = first.receive_json()
            snap_b = second.receive_json()
            assert snap_a["type"] == snap_b["type"] == "signals_snapshot"
            assert snap_a["seq"] == snap_b["seq"]
            assert set(snap_a["signals"]) == {cfg["id"] for cfg in _cache.config}

            client.portal.start_task_soon(_values_feed.tick)
            delta_a = first.receive_json()
            delta_b = second.receive_json()
            assert delta_a == delta_b  # одна рассылка, одинаковые сообщения
            assert delta_a["seq"] == snap_a["seq"] + 1
