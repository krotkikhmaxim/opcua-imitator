"""Взаимное исключение драйверов значений и шлюз heartbeat.

Реестр — данные: активен ровно один источник записей, конфликт — 409 с именем
чужого драйвера. Шлюз на время драйвера глушит heartbeat и возвращает его
только если тот был включён до драйвера. HTTP-слой проверяется на фейковом
движке: реальный цикл не нужен, чтобы поймать 409.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

import pytest
from fastapi.testclient import TestClient

from app.api.scenario_routes import (
    ensure_manual_writes_allowed,
    get_scenario_engine,
    set_scenario_engine,
)
from app.config import load_signal_config
from app.main import _registry, app
from app.services.drivers import (
    DriverConflict,
    HeartbeatGate,
    ValueDriverRegistry,
    WriteLedger,
)


@pytest.fixture(autouse=True)
def _isolate_shared_driver_state():
    """Файлы тестов делят холдер движка и реестр main: вернуть как было.

    Соседние файлы (test_recorded_modes, test_states) опираются на движок,
    установленный main при импорте, — оставлять после себя фейк нельзя.
    Драйвер «playback» не снимается, а перерегистрируется: его предикат
    (bus_held сервиса плейбэка) нужен соседним файлам — без него ручная
    запись перестаёт видеть идущий плейбэк.
    """
    from app.main import _playback

    original_engine = get_scenario_engine()
    yield
    set_scenario_engine(original_engine)
    _registry.register("playback", lambda: _playback.bus_held)


class FakeHeartbeat:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[bool] = []

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.calls.append(enabled)


class FakeEngine:
    def __init__(self, running: bool = False) -> None:
        self._running = running

    @property
    def running(self) -> bool:
        return self._running

    async def start(self, **kwargs):
        self._running = True
        return {"running": True}

    async def stop(self):
        self._running = False
        return {"running": False}


# -- реестр -----------------------------------------------------------------


def test_manual_write_is_blocked_only_while_a_driver_is_active():
    registry = ValueDriverRegistry()
    registry.register("scenario", lambda: False)
    registry.ensure_manual_writes_allowed()  # тихо
    registry.register("scenario", lambda: True)
    with pytest.raises(DriverConflict) as exc:
        registry.ensure_manual_writes_allowed()
    assert "scenario" in exc.value.message


def test_a_driver_cannot_start_while_another_is_active():
    registry = ValueDriverRegistry()
    playback_active = [False]
    registry.register("scenario", lambda: False)
    registry.register("playback", lambda: playback_active[0])

    registry.ensure_can_start("scenario")  # свободно
    playback_active[0] = True
    with pytest.raises(DriverConflict) as exc:
        registry.ensure_can_start("scenario")
    assert "playback" in exc.value.message
    registry.ensure_can_start("playback")  # сам себе не помеха


def test_active_lists_only_running_drivers_in_order():
    registry = ValueDriverRegistry()
    flags = {"a": [False], "b": [True]}
    registry.register("a", lambda: flags["a"][0])
    registry.register("b", lambda: flags["b"][0])
    assert registry.active() == ["b"]
    assert registry.any_active() is True
    flags["b"][0] = False
    assert registry.any_active() is False


# -- журнал записей ---------------------------------------------------------


def test_ledger_replays_writes_over_baseline_up_to_marker():
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)
    ledger = WriteLedger()

    async def writer(signal_id, value, when):
        return True

    wrapped = ledger.wrap(writer)

    async def scenario():
        await wrapped("pu-s1-avstop", False, t0)
        await wrapped("profibus-motor-speed", 0, t0 + timedelta(seconds=1))
        return ledger.expected_state_at(t0 + timedelta(seconds=2), {"pu-s1-avstop": True, "profibus-motor-speed": 42, "pu-u1-zero": True})

    state = asyncio.run(scenario())
    assert state == {"pu-s1-avstop": False, "profibus-motor-speed": 0, "pu-u1-zero": True}
    assert ledger.last_write("profibus-motor-speed")[1] == 0


def test_ledger_keeps_failed_writes_out():
    ledger = WriteLedger()

    async def failing(signal_id, value, when):
        return False

    asyncio.run(_write_and_check(ledger, failing))


async def _write_and_check(ledger, writer):
    from datetime import datetime, timezone

    wrapped = ledger.wrap(writer)
    ok = await wrapped("pu-s1-avstop", False, datetime.now(timezone.utc))
    assert ok is False
    assert ledger.expected_state_at(datetime.now(timezone.utc), {"pu-s1-avstop": True}) == {
        "pu-s1-avstop": True
    }


# -- шлюз heartbeat ---------------------------------------------------------


def test_gate_disables_heartbeat_for_a_driver_and_restores_it():
    async def scenario():
        registry = ValueDriverRegistry()
        heartbeat = FakeHeartbeat(enabled=True)
        gate = HeartbeatGate(registry, lambda: heartbeat)
        registry.register("scenario", lambda: False)
        await gate.refresh()
        assert heartbeat.enabled is True  # простой — шлюз не трогает

        registry.register("scenario", lambda: True)
        await gate.refresh()
        assert heartbeat.enabled is False  # драйвер активен — heartbeat выключен
        assert gate.holding is True

        registry.register("scenario", lambda: False)
        await gate.refresh()
        assert heartbeat.enabled is True  # возвращён, потому что был включён
        assert gate.holding is False

    asyncio.run(scenario())


def test_gate_does_not_enable_heartbeat_that_was_off_before():
    async def scenario():
        registry = ValueDriverRegistry()
        heartbeat = FakeHeartbeat(enabled=False)
        gate = HeartbeatGate(registry, lambda: heartbeat)
        registry.register("scenario", lambda: True)
        await gate.refresh()
        assert heartbeat.enabled is False
        registry.register("scenario", lambda: False)
        await gate.refresh()
        assert heartbeat.enabled is False  # был выключен до драйвера — так и остался

    asyncio.run(scenario())


def test_gate_is_silent_without_heartbeat_service():
    async def scenario():
        registry = ValueDriverRegistry()
        gate = HeartbeatGate(registry, lambda: None)
        registry.register("scenario", lambda: True)
        await gate.refresh()  # не падает
        assert gate.holding is False

    asyncio.run(scenario())


# -- HTTP-слой --------------------------------------------------------------


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_manual_write_conflicts_with_running_scenario(client):
    config = load_signal_config()
    signal_id = next(cfg["id"] for cfg in config)
    set_scenario_engine(FakeEngine(running=True))
    response = client.post(
        "/api/signals/write", json={"id": signal_id, "value": False}
    )
    assert response.status_code == 409
    assert "scenario" in response.json()["detail"]


def test_scenario_start_conflicts_with_running_playback(client):
    playback_active = [True]
    _registry.register("playback", lambda: playback_active[0])
    set_scenario_engine(FakeEngine(running=False))
    response = client.post(
        "/api/scenarios/start", json={"kinds": ["estop_console"], "once": True}
    )
    assert response.status_code == 409
    assert "playback" in response.json()["detail"]


def test_manual_write_after_stop_is_allowed_again(client):
    config = load_signal_config()
    signal_id = next(cfg["id"] for cfg in config)
    set_scenario_engine(FakeEngine(running=True))
    blocked = client.post("/api/signals/write", json={"id": signal_id, "value": False})
    assert blocked.status_code == 409
    set_scenario_engine(FakeEngine(running=False))
    allowed = client.post("/api/signals/write", json={"id": signal_id, "value": False})
    assert allowed.status_code == 200


def test_module_guard_delegates_to_registry():
    set_scenario_engine(FakeEngine(running=True))
    with pytest.raises(Exception) as exc:
        ensure_manual_writes_allowed()
    # фолбэк говорил бы «Идёт сценарий»; формулировка с id драйвера — путь реестра
    assert "«scenario»" in str(exc.value.detail)
