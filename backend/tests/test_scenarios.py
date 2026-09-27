"""Сценарии работы и аварий подъёмной машины.

Модель проверяется как данные: причина идёт раньше последствий, сбой,
видимый только в истории, действительно исчезает до остановки, штатная
остановка не трогает защиты, цикл возвращает установку в исправное
состояние. Движок — на виртуальном времени, без реального ожидания.
"""

import asyncio
import os
import random
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

import pytest
from fastapi.testclient import TestClient

from app.api.scenario_routes import set_scenario_engine
from app.config import load_signal_config
from app.main import app
from app.services.embedded_server import EmbeddedOpcUaServer
from app.services.scenario import ScenarioEngine, ScenarioError
from app.services.scenario.hoist import (
    BASELINE,
    DRIVE_EMERGENCY_OFF,
    ENCODERS,
    ESTOP_CONSOLE,
    INV_FAULT,
    INV_STATUS,
    KINDS,
    MOTOR_SPEED,
    SYNC_LEFT,
    SYNC_RIGHT,
    TP_RELAY,
    HoistState,
    _full_trip,
    build_cycle,
)
from app.services.scenario.timeline import TICK_MS, Timeline

CONFIG = load_signal_config()
IDS = [cfg["id"] for cfg in CONFIG]
FAULT_KINDS = [kind for kind, item in KINDS.items() if item.fault]
PROTECTIVE = {ESTOP_CONSOLE, DRIVE_EMERGENCY_OFF, TP_RELAY}


def _cycle(kind, seed=7):
    return build_cycle(kind, random.Random(seed), HoistState(), dict(BASELINE), IDS)


def _value_at(changes, signal_id, at_ms, initial):
    value = initial
    for change in changes:
        if change.at_ms > at_ms:
            break
        if change.signal_id == signal_id:
            value = change.value
    return value


def test_baseline_covers_every_published_signal():
    assert set(BASELINE) == set(IDS)


def test_cycle_is_deterministic_for_a_seed():
    first, _ = _cycle("estop_console", seed=42)
    second, _ = _cycle("estop_console", seed=42)
    assert first == second


def test_every_change_sits_on_the_100ms_grid():
    for kind in KINDS:
        cycle, _ = _cycle(kind)
        assert all(change.at_ms % TICK_MS == 0 for change in cycle.changes)


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_the_cause_precedes_the_stop(kind):
    cycle, _ = _cycle(kind)
    truth = cycle.truth
    changes = cycle.changes
    # машина шла полным ходом до причины и стоит после остановки
    assert _value_at(changes, MOTOR_SPEED, truth.cause_at_ms - TICK_MS, 0) != 0
    assert _value_at(changes, MOTOR_SPEED, truth.stopped_at_ms, None) == 0
    assert truth.cause_at_ms < truth.stopped_at_ms < truth.recovered_at_ms
    # причина записана ровно в момент причины
    for cause in truth.causes:
        assert any(
            c.at_ms == truth.cause_at_ms and c.signal_id == cause.signal_id and c.value == cause.after
            for c in changes
        )
    # реле ТП отпадает не раньше причины
    drop = min(c.at_ms for c in changes if c.signal_id == TP_RELAY and c.value is False)
    assert drop >= truth.cause_at_ms


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_no_protective_input_moves_before_the_cause_in_the_episode(kind):
    cycle, _ = _cycle(kind)
    truth = cycle.truth
    window = [c for c in cycle.changes if truth.cause_at_ms - 5000 <= c.at_ms < truth.cause_at_ms]
    assert not [c for c in window if c.signal_id in PROTECTIVE]


def test_glitch_is_gone_before_the_machine_stops():
    cycle, _ = _cycle("estop_circuit_glitch")
    truth = cycle.truth
    assert not truth.cause_visible_after_stop
    assert _value_at(cycle.changes, ESTOP_CONSOLE, truth.cause_at_ms, True) is False
    assert _value_at(cycle.changes, ESTOP_CONSOLE, truth.stopped_at_ms, None) is True


def test_persistent_estop_stays_open_while_the_machine_stands():
    cycle, _ = _cycle("estop_console")
    truth = cycle.truth
    assert truth.cause_visible_after_stop
    assert _value_at(cycle.changes, ESTOP_CONSOLE, truth.stopped_at_ms + 10000, None) is False


def test_inverter_fault_shows_an_overcurrent_before_the_fault_bit():
    cycle, _ = _cycle("inverter_fault")
    truth = cycle.truth
    currents = [
        c.value
        for c in cycle.changes
        if c.signal_id == "profibus-current-filtered" and truth.cause_at_ms - 600 <= c.at_ms < truth.cause_at_ms
    ]
    assert currents == sorted(currents) and currents[-1] > 15000
    status = _value_at(cycle.changes, INV_STATUS, truth.cause_at_ms, 0)
    assert status & INV_FAULT


def test_normal_stop_touches_no_protection():
    cycle, _ = _cycle("normal_stop")
    assert not cycle.truth.fault
    assert not [c for c in cycle.changes if c.signal_id in PROTECTIVE]
    assert not [c for c in cycle.changes if c.signal_id == INV_STATUS and c.value & INV_FAULT]


@pytest.mark.parametrize("kind", list(KINDS))
def test_a_cycle_returns_the_plant_to_the_healthy_state(kind):
    _, end = _cycle(kind)
    moving = set(ENCODERS) | set(SYNC_LEFT) | set(SYNC_RIGHT)
    assert {k: v for k, v in end.items() if k not in moving} == {
        k: v for k, v in BASELINE.items() if k not in moving
    }


def test_a_round_trip_brings_the_encoders_back():
    state = HoistState()
    timeline = Timeline(dict(BASELINE), IDS)
    t = _full_trip(timeline, state, 0, random.Random(1))
    _full_trip(timeline, state, t, random.Random(2))
    assert state.encoders == [0, 0, 0, 0] and state.left_at_top


# -- движок на виртуальном времени -------------------------------------------


class _VirtualTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _engine(tmp_path, writes, configs=CONFIG, simulated=True):
    clock = _VirtualTime()

    async def writer(signal_id, value, when):
        writes.append((signal_id, value, when))
        return True

    engine = ScenarioEngine(
        writer,
        configs,
        simulated=simulated,
        journal_path=tmp_path / "journal.jsonl",
        sleep=clock.sleep,
        clock=lambda: T0,
        monotonic=clock.monotonic,
    )
    return engine


def test_engine_plays_baseline_then_the_cycle_with_scenario_timestamps(tmp_path):
    writes = []
    engine = _engine(tmp_path, writes)

    async def run():
        await engine.start(kinds=["estop_console"], seed=3, once=True)
        await engine._task

    asyncio.run(run())
    baseline = writes[: len(BASELINE)]
    assert {signal_id for signal_id, _, _ in baseline} == set(BASELINE)
    assert all(when == T0 for _, _, when in baseline)
    stamps = [when for _, _, when in writes]
    assert stamps == sorted(stamps)
    assert all((when - T0) % timedelta(milliseconds=TICK_MS) == timedelta(0) for when in stamps)
    status = engine.status()
    assert status["phase"] == "finished" and not status["running"]
    assert status["write_failures"] == 0

    (record,) = engine.journal()
    assert record["kind"] == "estop_console" and record["fault"] is True
    cause = record["causes"][0]
    assert cause["signal_id"] == ESTOP_CONSOLE
    assert cause["project_tag"] and cause["name"]
    # момент причины в журнале совпадает с моментом записи причины
    cause_writes = [when for sid, value, when in writes if sid == ESTOP_CONSOLE and value is False]
    assert record["cause_at"] == cause_writes[0].isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert (tmp_path / "journal.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_engine_refuses_the_real_mode(tmp_path):
    engine = _engine(tmp_path, [], simulated=False)
    assert not engine.available

    async def run():
        with pytest.raises(ScenarioError) as info:
            await engine.start()
        assert info.value.code == "real_mode"

    asyncio.run(run())


def test_engine_needs_the_model_signals(tmp_path):
    legacy = [{"id": "signal_1", "name": "x", "type": "float"}]
    engine = _engine(tmp_path, [], configs=legacy)
    assert not engine.available
    assert engine.describe()["unavailable_reason"] == "missing_signals"


def test_engine_stop_cancels_a_running_loop():
    async def run():
        never = asyncio.Event()

        async def blocking_sleep(delay):
            await never.wait()

        async def writer(signal_id, value, when):
            return True

        engine = ScenarioEngine(
            writer,
            CONFIG,
            simulated=True,
            sleep=blocking_sleep,
            clock=lambda: T0,
            monotonic=lambda: 0.0,
        )
        await engine.start(seed=1)
        await asyncio.sleep(0)
        assert engine.running
        status = await engine.stop()
        assert not status["running"] and status["phase"] == "stopped"

    asyncio.run(run())


def test_unknown_kind_is_rejected(tmp_path):
    engine = _engine(tmp_path, [])

    async def run():
        with pytest.raises(ScenarioError) as info:
            await engine.start(kinds=["no_such_fault"])
        assert info.value.code == "unknown_kind"

    asyncio.run(run())


# -- HTTP API ----------------------------------------------------------------


class _RunningEngine:
    running = True


def test_api_lists_scenarios_and_assumptions():
    client = TestClient(app)
    body = client.get("/api/scenarios").json()
    assert body["available"] is True
    assert {item["kind"] for item in body["kinds"]} == set(KINDS)
    assert body["assumptions"]


def test_api_rejects_an_unknown_kind():
    client = TestClient(app)
    response = client.post("/api/scenarios/start", json={"kinds": ["no_such_fault"]})
    assert response.status_code == 400


def test_manual_writes_are_blocked_while_a_scenario_runs():
    from app.api import scenario_routes

    client = TestClient(app)
    original = scenario_routes.get_scenario_engine()
    set_scenario_engine(_RunningEngine())
    try:
        assert client.post("/api/signals/write", json={"id": ESTOP_CONSOLE, "value": False}).status_code == 409
        assert client.post("/api/signals/write_batch", json={"values": {ESTOP_CONSOLE: False}}).status_code == 409
        assert client.post("/api/states/load", json={"filename": "x.json"}).status_code == 409
    finally:
        set_scenario_engine(original)


# -- встроенный сервер -------------------------------------------------------


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_embedded_server_writes_the_scenario_timestamp():
    async def run():
        server = EmbeddedOpcUaServer(
            [{"id": "P.MotorSpeed", "name": "Скорость", "type": "Int16", "writable": True, "default": 0}],
            endpoint=f"opc.tcp://127.0.0.1:{_free_port()}",
        )
        await server.start()
        try:
            when = datetime(2026, 9, 26, 12, 0, 0, 300000, tzinfo=timezone.utc)
            assert await server.write_value_at("P.MotorSpeed", 1234, when)
            data = await server.nodes["P.MotorSpeed"].read_data_value()
            assert data.Value.Value == 1234
            assert data.SourceTimestamp.replace(tzinfo=timezone.utc) == when
        finally:
            await server.stop()

    asyncio.run(run())
