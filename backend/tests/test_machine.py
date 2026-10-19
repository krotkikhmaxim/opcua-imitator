"""Пульт машины: состояние из записи, воздействие из цепочки защиты.

Проверяется то, что видит оператор: кадр состояния приходит из реальной
записи телеметрии, воздействие меняет сигналы, а строка состояния выводится
из фактических значений, а не из факта нажатия. Одно активное воздействие,
завершение и «начать заново» проверяются как правила, а не как кнопки.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPC_UA_MODE", "imitator")

import pytest
from fastapi.testclient import TestClient

from app.config import load_signal_config
from app.services.machine import EVENTS, STATES, MachineError, MachineSession
from app.services.ownership import OwnershipRegistry
from app.services.scenario.hoist import (
    BASELINE,
    DRIVE_EMERGENCY_OFF,
    ESTOP_CONSOLE,
    INV_FAULT_ID,
    INV_STATUS,
    KAR_DOWN,
    KAR_ZERO,
    KRT_BRAKED,
    KRT_RELEASED,
    MOTOR_SPEED,
    TP_RELAY,
)

CONFIG = load_signal_config()
T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Виртуальное время: sleep двигает и настенные часы, и монотонные."""

    def __init__(self) -> None:
        self.now = T0
        self.mono = 0.0

    def wall(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, delay: float) -> None:
        self.now += timedelta(seconds=delay)
        self.mono += delay


class Store:
    """Шина-двойник: значения сигналов и все записи пульта."""

    def __init__(self) -> None:
        self.values = dict(BASELINE)
        self.writes = []

    async def read(self, signal_id):
        return self.values.get(signal_id)

    def writer_factory(self, session_id):
        async def writer(signal_id, value, when):
            self.writes.append((session_id, signal_id, value, when))
            self.values[signal_id] = value
            return True

        return writer


def _session(store=None, ownership=None):
    store = store or Store()
    ownership = ownership if ownership is not None else OwnershipRegistry()
    clock = FakeClock()
    session = MachineSession(
        store.writer_factory,
        CONFIG,
        read_value=store.read,
        ownership=ownership,
        sleep=clock.sleep,
        clock=clock.wall,
        monotonic=clock.monotonic,
    )
    return session, store, ownership


def _run(coro):
    return asyncio.run(coro)


# -- состояния --------------------------------------------------------------


@pytest.mark.parametrize(
    "state,expected",
    [
        ("off", {TP_RELAY: False, INV_STATUS: 1, MOTOR_SPEED: 0}),
        ("ready", {TP_RELAY: True, INV_STATUS: 1025, MOTOR_SPEED: 0}),
        ("moving", {TP_RELAY: True, INV_STATUS: 1058, MOTOR_SPEED: 1689}),
    ],
)
def test_state_frame_matches_the_recorded_telemetry(state, expected):
    """Кадр состояния — реальные значения записи, а не выдуманный набор."""

    async def run():
        session, store, _ = _session()
        await session.start(state)
        for signal_id, value in expected.items():
            assert store.values[signal_id] == value

    _run(run())


def test_moving_state_derives_the_operator_frame():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        status = await session.status()
        assert status["headline"] == "Машина в движении"
        assert status["observed"]["speed"] == 1689
        assert status["state"] == "moving"
        assert store.values[KRT_RELEASED] is True
        assert store.values[KRT_BRAKED] is False
        assert store.values[KAR_ZERO] is False
        assert store.values[KAR_DOWN] is True

    _run(run())


def test_off_and_ready_states_differ_in_facts():
    async def run():
        session, _, _ = _session()
        off = await session.start("off")
        assert off["headline"] == "Машина отключена"
        await session.finish()
        ready = await session.start("ready")
        assert ready["headline"] == "Машина стоит"

    _run(run())


# -- воздействия ------------------------------------------------------------


def test_estop_press_stops_machine_and_reports_actual_facts():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        status = await session.event("estop_press")
        assert store.values[ESTOP_CONSOLE] is False
        assert store.values[TP_RELAY] is False
        assert store.values[MOTOR_SPEED] == 0
        assert status["headline"] == "Машина остановлена"
        assert "останова" in status["detail"]
        assert status["active_event"]["title"] == "Аварийная кнопка нажата"
        assert status["active_event"]["remedy"] == "Отпустить аварийную кнопку"
        assert [entry["kind"] for entry in status["history"]][0] == "estop_press"

    _run(run())


def test_estop_remedy_releases_the_button_and_charges_the_relay():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        await session.event("estop_press")
        status = await session.remedy()
        assert store.values[ESTOP_CONSOLE] is True
        assert store.values[TP_RELAY] is True
        assert store.values[MOTOR_SPEED] == 0
        assert status["active_event"] is None
        assert status["notice"]["text"] == "аварийная кнопка отпущена, реле ТП взведено"
        assert status["headline"] == "Машина стоит"

    _run(run())


def test_momentary_break_is_over_but_protection_has_tripped():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        status = await session.event("estop_glitch")
        assert store.values[ESTOP_CONSOLE] is True  # цепь восстановлена
        assert store.values[TP_RELAY] is False  # но защита уже сработала
        assert store.values[MOTOR_SPEED] == 0
        assert status["active_event"] is None
        assert status["notice"]["text"] == "Кратковременный разрыв выполнен. Цепь восстановлена."
        assert status["notice"]["repeat_event"] == "estop_glitch"

    _run(run())


def test_normal_stop_touches_no_protection():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        status = await session.event("normal_stop")
        assert store.values[TP_RELAY] is True
        assert store.values[ESTOP_CONSOLE] is True
        assert store.values[MOTOR_SPEED] == 0
        assert store.values[KRT_BRAKED] is True
        assert status["notice"]["text"] == "Машина остановлена штатно, защиты не срабатывали."

    _run(run())


def test_inverter_fault_sets_the_code_and_remedy_clears_it():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        status = await session.event("inverter_fault")
        assert store.values[INV_FAULT_ID] == 7
        assert store.values[TP_RELAY] is False
        assert status["observed"]["drive_fault_text"] == "код 7"
        assert status["active_event"]["remedy"] == "Квитировать аварию привода"
        await session.remedy()
        assert store.values[INV_FAULT_ID] == 0
        assert store.values[TP_RELAY] is True

    _run(run())


def test_only_one_active_event_at_a_time():
    async def run():
        session, _, _ = _session()
        await session.start("moving")
        await session.event("estop_press")
        with pytest.raises(MachineError) as info:
            await session.event("estop_press")
        assert info.value.code == "event_active"
        assert "устраните" in info.value.message

    _run(run())


def test_event_availability_follows_the_state():
    async def run():
        session, _, _ = _session()
        off = await session.start("off")
        assert not any(event["available"] for event in off["events"])
        assert off["events_hint"] == "Машина отключена: выберите другое состояние и нажмите «Запустить»"
        with pytest.raises(MachineError):
            await session.event("estop_press")

        await session.finish()
        ready = await session.start("ready")
        available = {event["kind"]: event["available"] for event in ready["events"]}
        assert available["estop_press"] is True
        assert available["drive_emergency_off"] is True
        assert available["normal_stop"] is False
        assert available["tp_relay_dropout"] is False
        with pytest.raises(MachineError) as info:
            await session.event("normal_stop")
        assert info.value.code == "not_applicable"

    _run(run())


def test_status_comes_from_signals_not_from_the_pressed_button():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        status = await session.event("estop_press")
        assert status["headline"] == "Машина остановлена"
        # Сигналы вернули в норму мимо пульта — статус обязан это показать.
        store.values[ESTOP_CONSOLE] = True
        store.values[TP_RELAY] = True
        again = await session.status()
        assert again["headline"] == "Машина стоит"
        assert again["active_event"] is not None  # воздействие пульта ещё «активно»

    _run(run())


# -- жизненный цикл ---------------------------------------------------------


def test_start_while_running_is_rejected():
    async def run():
        session, _, _ = _session()
        await session.start("moving")
        with pytest.raises(MachineError) as info:
            await session.start("ready")
        assert info.value.code == "already_started"

    _run(run())


def test_finish_keeps_values_and_blocks_new_events():
    async def run():
        session, store, ownership = _session()
        await session.start("moving")
        status = await session.finish()
        assert status["phase"] == "finished"
        assert status["can_finish"] is False
        assert status["can_start"] is True
        assert store.values[MOTOR_SPEED] == 1689  # последние значения на месте
        assert ownership.held_signals() == {}
        with pytest.raises(MachineError) as info:
            await session.event("normal_stop")
        assert info.value.code == "finished"

    _run(run())


def test_restart_clears_history_and_restores_the_state():
    async def run():
        session, store, _ = _session()
        await session.start("moving")
        await session.event("normal_stop")
        assert store.values[MOTOR_SPEED] == 0
        status = await session.restart()
        assert store.values[MOTOR_SPEED] == 1689
        assert status["active_event"] is None
        assert status["notice"] is None
        assert [entry["kind"] for entry in status["history"]] == ["start"]
        assert status["last_action"] is None

    _run(run())


def test_ownership_is_held_for_the_session():
    async def run():
        session, _, ownership = _session()
        await session.start("moving")
        held = ownership.held_signals()
        assert TP_RELAY in held
        assert held[TP_RELAY].kind == "machine"
        await session.finish()
        assert ownership.held_signals() == {}

    _run(run())


def test_restart_after_finish_takes_ownership_back():
    async def run():
        session, store, ownership = _session()
        await session.start("ready")
        await session.finish()
        assert ownership.held_signals() == {}
        status = await session.restart("moving")
        assert store.values[MOTOR_SPEED] == 1689
        assert status["phase"] == "running"
        held = ownership.held_signals()
        assert held[TP_RELAY].id.startswith("machine:")

    _run(run())


def test_busy_rejects_a_second_action():
    async def run():
        session, _, _ = _session()
        await session.start("moving")
        session._session.busy = True
        with pytest.raises(MachineError) as info:
            await session.event("estop_press")
        assert info.value.code == "busy"

    _run(run())


# -- HTTP ------------------------------------------------------------------


def test_api_exposes_states_and_events():
    from app.main import app

    client = TestClient(app)
    body = client.get("/api/machine").json()
    assert [state["kind"] for state in body["states"]] == list(STATES)
    assert [event["kind"] for event in body["events"]] == list(EVENTS)
    assert body["events_hint"] == "Сначала выберите состояние и нажмите «Запустить»"


def test_api_off_state_rejects_effects_and_finishes():
    from app.main import app

    client = TestClient(app)
    started = client.post("/api/machine/start", json={"state": "off"})
    assert started.status_code == 200
    assert started.json()["state"] == "off"
    rejected = client.post("/api/machine/event", json={"kind": "estop_press"})
    assert rejected.status_code == 409
    assert "отключена" in rejected.json()["detail"].lower()
    finished = client.post("/api/machine/finish")
    assert finished.status_code == 200
    assert finished.json()["phase"] == "finished"
