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

    # Писец у каждого прогона свой (фабрика по run_id); журнал общий.
    def writer_factory(run_id):
        async def writer(signal_id, value, when):
            writes.append((signal_id, value, when))
            return True

        return writer

    engine = ScenarioEngine(
        writer_factory,
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
        status = await engine.start(kinds=["estop_console"], seed=3, once=True)
        await engine._runs[status["run_id"]].task

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
            lambda run_id: writer,
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


# -- владение и несколько прогонов (ТЗ #5 §9.1, §9.2) ------------------------


class _OneShotGate:
    """Виртуальное время с однократным шлюзом: один sleep застревает, пока
    шлюз не открыт, — окно для паузы, конфликта и параллельного прогона."""

    def __init__(self):
        self.now = 0.0
        self.armed = False
        self.release = asyncio.Event()

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        if self.armed:
            self.armed = False
            await self.release.wait()


def _gated_engine(writes, gate):
    def writer_factory(run_id):
        async def writer(signal_id, value, when):
            writes.append((run_id, signal_id, value, when))
            return True

        return writer

    from app.services.ownership import OwnershipRegistry

    return ScenarioEngine(
        writer_factory,
        CONFIG,
        simulated=True,
        ownership=OwnershipRegistry(),
        sleep=gate.sleep,
        clock=lambda: T0,
        monotonic=gate.monotonic,
    )


def test_intersecting_start_is_rejected_and_the_first_run_survives():
    """Пересекающийся запуск отклоняется ЦЕЛИКОМ с перечнем, первый жив."""
    writes = []

    async def run():
        gate = _OneShotGate()
        engine = _gated_engine(writes, gate)
        gate.armed = True
        first = await engine.start(kinds=["normal_stop"], seed=1, once=True)
        await asyncio.sleep(0)
        assert engine.running

        with pytest.raises(ScenarioError) as info:
            await engine.start(kinds=["estop_console"], seed=2)
        assert info.value.code == "signals_busy"
        assert MOTOR_SPEED in info.value.message  # конкретный сигнал в перечне
        assert "normal_stop" in info.value.message  # и титул владельца
        assert set(engine._runs) == {first["run_id"]}  # второй не создан
        assert engine.running  # первый не тронут

        gate.release.set()
        await engine._runs[first["run_id"]].task
        assert engine.status()["phase"] == "finished"

    asyncio.run(run())


def test_two_disjoint_declared_sets_run_concurrently(tmp_path, monkeypatch):
    """Непересекающиеся наборы играют одновременно; владение — объединение."""
    from app.services.ownership import OwnershipRegistry
    from app.services.scenario import engine as engine_module

    writes = []

    async def run():
        gate = _OneShotGate()
        engine = _gated_engine(writes, gate)
        first_set = {MOTOR_SPEED, ESTOP_CONSOLE}
        monkeypatch.setattr(engine_module, "BASELINE", {MOTOR_SPEED: 0, ESTOP_CONSOLE: True})
        gate.armed = True
        first = await engine.start(kinds=["normal_stop"], seed=1, once=True)
        await asyncio.sleep(0)

        # пересечение по MOTOR_SPEED — конфликт; {TP_RELAY} — не пересекается
        monkeypatch.setattr(engine_module, "BASELINE", {MOTOR_SPEED: 0})
        with pytest.raises(ScenarioError):
            await engine.start(kinds=["normal_stop"], seed=2, once=True)
        monkeypatch.setattr(engine_module, "BASELINE", {TP_RELAY: False})
        second = await engine.start(kinds=["normal_stop"], seed=3, once=True)

        assert first["run_id"] != second["run_id"]
        cards = {card["run_id"]: card for card in second["runs"]}
        assert set(cards) == {first["run_id"], second["run_id"]}
        assert cards[first["run_id"]]["owned_signals"] == sorted(first_set)
        assert cards[second["run_id"]]["owned_signals"] == [TP_RELAY]
        held = engine._ownership.held_signals()
        assert held[MOTOR_SPEED].id == f"scenario:{first['run_id']}"
        assert held[TP_RELAY].id == f"scenario:{second['run_id']}"

        gate.release.set()
        await engine._runs[first["run_id"]].task
        await engine._runs[second["run_id"]].task
        assert engine._ownership.held_signals() == {}
        assert engine.status()["running"] is False

    asyncio.run(run())


def test_pause_keeps_ownership_and_resume_keeps_intervals_exact():
    """Пауза (§9.1): смены стоят, владение при прогоне; продолжение сдвигает
    базу времени на длительность паузы — интервалы сценария не плывут."""
    writes = []

    async def run():
        gate = _OneShotGate()
        engine = _gated_engine(writes, gate)
        gate.armed = True
        status = await engine.start(kinds=["normal_stop"], seed=7, once=True)
        run_id = status["run_id"]
        await asyncio.sleep(0)
        run = engine._runs[run_id]
        t0_mono_before = run.t0_mono

        await engine.pause(run_id)
        assert engine.status()["phase"] == "paused"
        # пауза сохраняет владение — ручная правка не меняет условия продолжения
        held = engine._ownership.owner_of(ESTOP_CONSOLE)
        assert held is not None and held.id == f"scenario:{run_id}"

        gate.now += 5.0  # пять секунд паузы в виртуальном времени
        gate.armed = False
        await engine.resume(run_id)
        assert run.t0_mono == pytest.approx(t0_mono_before + 5.0)
        assert abs(run.t0_wall - (T0 + timedelta(seconds=5))) < timedelta(milliseconds=1)
        assert engine.status()["phase"] in ("normal", "episode", "stopped")

        gate.release.set()
        await run.task
        assert engine.status()["phase"] == "finished"
        stamps = [when for _, _, _, when in writes]
        assert stamps == sorted(stamps)  # назад во времени не пишем
        assert engine._ownership.owner_of(ESTOP_CONSOLE) is None

    asyncio.run(run())


def test_error_keeps_ownership_until_explicit_stop(tmp_path):
    """Ошибка ставит прогон с владением; набор освобождает только решение
    оператора — остановка (§9.2)."""
    calls = {"n": 0}

    async def writer(signal_id, value, when):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("шина отказала")
        return True

    async def run():
        from app.services.ownership import OwnershipRegistry

        engine = ScenarioEngine(
            lambda run_id: writer,
            CONFIG,
            simulated=True,
            ownership=OwnershipRegistry(),
            clock=lambda: T0,
            monotonic=lambda: 0.0,
        )
        status = await engine.start(kinds=["normal_stop"], seed=1, once=True)
        run_id = status["run_id"]
        await engine._runs[run_id].task
        assert engine.status()["phase"] == "error"
        assert engine.status()["error"] == "RuntimeError"

        held = engine._ownership.owner_of(MOTOR_SPEED)
        assert held is not None and held.id == f"scenario:{run_id}"
        assert held.kind == "scenario"

        result = await engine.stop(run_id)
        assert result["phase"] == "stopped"
        assert engine._ownership.owner_of(MOTOR_SPEED) is None

    asyncio.run(run())


# -- HTTP API ----------------------------------------------------------------


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


def test_manual_writes_to_owned_signals_are_rejected_others_pass():
    """ТЗ #5 §9.2: вместо грубой блокировки — владение по-сигнально.

    Занятый сценарием сигнал: ручная запись 409 с именем владельца, пакет с
    ним отклоняется целиком (код owned). Свободные сигналы ручная запись
    меняет. Восстановление состояния больше не запирается целиком — занятые
    сигналы отклонит механизм, остальное применится (здесь — файл не найден,
    404 вместо прежнего 409).
    """
    from app.main import _cache, _ownership
    from app.services.ownership import Owner

    client = TestClient(app)
    writable = [cfg["id"] for cfg in _cache.config if cfg.get("writable")]
    assert len(writable) >= 2
    held, free = writable[0], writable[1]
    owner = Owner(id="scenario:testrun01", kind="scenario", title="Сценарий-тест")
    assert _ownership.acquire(owner, [held]) == []
    try:
        response = client.post("/api/signals/write", json={"id": held, "value": True})
        assert response.status_code == 409
        assert "Сценарий-тест" in response.json()["detail"]
        assert client.post("/api/signals/write", json={"id": free, "value": True}).status_code == 200

        body = client.post(
            "/api/signals/write_batch", json={"values": {free: False, held: True}}
        ).json()
        assert body["status"] == "rejected"
        assert body["results"][held]["code"] == "owned"
        assert body["results"][free]["code"] == "package_rejected"

        assert client.post("/api/states/load", json={"filename": "x.json"}).status_code == 404
    finally:
        _ownership.release(owner.id)
    assert client.post("/api/signals/write", json={"id": held, "value": True}).status_code == 200


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
