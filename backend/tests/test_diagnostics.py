"""Диагностика, отказы и восстановление (ТЗ #5, шаг 9 — §14, §16).

Покрывает: ограниченный журнал операций (op_id, источник, сигналы,
результат, ошибки, длительность, ревизии), события жизненного цикла
сценария (старт/отказ с конфликтами/пауза/продолжение/остановка/
завершение/ошибка с шагом/освобождение владения), вытеснение старых
прогонов, операции через API (успех и отказ), интеграцию с реальными
OPC UA-узлами через независимый клиент, честный отказ записи (fault
injection) и семантику аварийного перезапуска (последний валидный снимок).
"""

import asyncio
import os
import socket

os.environ.setdefault("OPC_UA_MODE", "imitator")  # до импорта app.main

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import load_signal_config
from app.api.diagnostics_routes import set_diagnostics_journal
from app.main import _diagnostics, app
from app.services.diagnostics import DiagnosticsJournal
from app.services.ownership import Owner, OwnershipRegistry
from app.services.persistence import SnapshotStore
from app.services.scenario import ScenarioEngine
from app.services.signal_cache import SignalCache

CONFIG = load_signal_config()


# -- журнал: ограниченность и состав записи (§14) -------------------------------


def test_journal_is_bounded_and_newest_first():
    journal = DiagnosticsJournal(limit=3)
    for i in range(5):
        journal.record_scenario("started", run_id=f"r{i}")
    assert len(journal) == 3  # старейшие вытеснены — роста без ограничений нет
    tails = journal.tail(10)
    assert [e["run_id"] for e in tails] == ["r4", "r3", "r2"]  # новые первыми


def test_operation_record_carries_all_diagnostics_fields():
    journal = DiagnosticsJournal()
    journal.record_operation(
        op_id="op-1",
        source="manual_batch",
        status="partial",
        signals=["a", "b", "c"],
        errors=["Сигнал c только для чтения"],
        duration_ms=12.345,
        revisions={"a": 7, "b": 3},
    )
    (entry,) = journal.tail(1)
    assert entry["kind"] == "operation"
    assert entry["op_id"] == "op-1"
    assert entry["source"] == "manual_batch"
    assert entry["status"] == "partial"
    assert entry["signals"] == ["a", "b", "c"]
    assert entry["errors"] == ["Сигнал c только для чтения"]
    assert entry["duration_ms"] == 12.345
    assert entry["revisions"] == {"a": 7, "b": 3}
    datetime.fromisoformat(entry["ts"])  # время выполнения зафиксировано


# -- события жизненного цикла сценария (§14) ------------------------------------


class _VirtualTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


class _OneShotGate:
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


def _wired_engine(tmp_path, events, ownership=None, gate=None, run_limit=50):
    def writer_factory(run_id):
        async def writer(signal_id, value, when):
            return True

        return writer

    if gate is not None:
        timing = {"sleep": gate.sleep, "monotonic": gate.monotonic}
    else:
        clock = _VirtualTime()
        timing = {"sleep": clock.sleep, "monotonic": clock.monotonic}
    return ScenarioEngine(
        writer_factory,
        CONFIG,
        simulated=True,
        ownership=ownership if ownership is not None else OwnershipRegistry(),
        journal_path=tmp_path / "journal.jsonl",
        on_event=events.append,
        run_limit=run_limit,
        **timing,
    )


def test_engine_emits_started_finished_and_ownership_released(tmp_path):
    events = []
    engine = _wired_engine(tmp_path, events)

    async def run():
        status = await engine.start(kinds=["normal_stop"], seed=1, once=True)
        await engine._runs[status["run_id"]].task

    asyncio.run(run())
    names = [e["event"] for e in events]
    assert names == ["started", "finished", "ownership_released"]
    started, finished, released = events
    assert started["signals"] == sorted(started["signals"])  # перечень владения
    assert len(started["signals"]) > 0 and started["run_id"]
    assert finished["writes"] > 0 and finished["write_failures"] == 0
    assert finished["episodes"] >= 1
    assert len(released["signals"]) == len(started["signals"])  # набор возвращён целиком


def test_engine_emits_start_rejected_with_conflict_listing(tmp_path):
    events = []
    registry = OwnershipRegistry()
    engine = _wired_engine(tmp_path, events, ownership=registry)
    registry.acquire(Owner(id="manual:x", kind="manual", title="Ручной"), ["profibus-motor-speed"])

    async def run():
        try:
            await engine.start(kinds=["normal_stop"], seed=2, once=True)
            raise AssertionError("пересечение не отклонено")
        except Exception as exc:
            assert getattr(exc, "code", "") == "signals_busy"

    asyncio.run(run())
    (rejected,) = [e for e in events if e["event"] == "start_rejected"]
    assert rejected["code"] == "signals_busy"
    assert rejected["conflicts"][0]["signal_id"] == "profibus-motor-speed"
    assert rejected["conflicts"][0]["owner_id"] == "manual:x"
    assert rejected["conflicts"][0]["owner_title"] == "Ручной"
    assert not any(e["event"] == "started" for e in events)


def test_engine_emits_paused_resumed_and_stopped(tmp_path):
    events = []
    gate = _OneShotGate()
    gate.armed = True
    engine = _wired_engine(tmp_path, events, gate=gate)

    async def run():
        status = await engine.start(kinds=["normal_stop"], seed=3, once=True)
        await asyncio.sleep(0)
        await engine.pause(status["run_id"])
        await engine.resume(status["run_id"])
        gate.release.set()
        await engine._runs[status["run_id"]].task

    asyncio.run(run())
    names = [e["event"] for e in events]
    assert "paused" in names and "resumed" in names
    resumed = next(e for e in events if e["event"] == "resumed")
    assert resumed["pause_s"] >= 0
    assert names[-1] == "ownership_released"
    assert names[-2] == "finished"


def test_engine_error_event_carries_the_step_and_stop_acknowledges(tmp_path):
    events = []

    def writer_factory(run_id):
        async def writer(signal_id, value, when):
            raise RuntimeError("opc ua gone")

        return writer

    clock = _VirtualTime()
    engine = ScenarioEngine(
        writer_factory,
        CONFIG,
        simulated=True,
        ownership=OwnershipRegistry(),
        journal_path=tmp_path / "journal.jsonl",
        on_event=events.append,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    async def run():
        status = await engine.start(kinds=["normal_stop"], seed=4, once=True)
        await engine._runs[status["run_id"]].task
        assert engine.status()["phase"] == "error"
        await engine.stop()  # явное решение оператора
        assert engine.status()["phase"] == "stopped"

    asyncio.run(run())
    (error,) = [e for e in events if e["event"] == "error"]
    assert error["error"] == "RuntimeError"
    assert "opc ua gone" in error["detail"]
    assert error["episode"] == 0  # шаг: пал на базисе, до эпизодов не дошёл
    stopped = next(e for e in events if e["event"] == "stopped")
    assert stopped["error_acknowledged"] is True
    assert events[-1]["event"] == "ownership_released"  # остановка освобождает


def test_finished_runs_are_pruned_beyond_the_limit(tmp_path):
    events = []
    engine = _wired_engine(tmp_path, events, run_limit=3)

    async def run():
        for seed in range(5):
            status = await engine.start(kinds=["normal_stop"], seed=seed, once=True)
            await engine._runs[status["run_id"]].task

    asyncio.run(run())
    assert len(engine._runs) == 3  # §14: память не растёт без ограничений
    assert [run.status["seed"] for run in engine._runs.values()] == [2, 3, 4]


# -- операции через API (§14: успех и отказ фиксируются) ------------------------


def _writable():
    return next(cfg for cfg in CONFIG if cfg.get("writable"))


def test_write_records_the_operation_with_op_id_and_revisions():
    cfg = _writable()
    client = TestClient(app)
    response = client.post("/api/signals/write", json={"id": cfg["id"], "value": True, "op_id": "diag-1"})
    assert response.status_code == 200
    op_id = response.json()["op_id"]

    events = client.get("/api/diagnostics/events?limit=50").json()["events"]
    (entry,) = [e for e in events if e.get("op_id") == "diag-1"]
    assert entry["kind"] == "operation"
    assert entry["source"] == "manual"
    assert entry["status"] == "applied"
    assert entry["signals"] == [cfg["id"]]
    assert entry["revisions"][cfg["id"]] == response.json()["revision"]
    assert entry["duration_ms"] >= 0


def test_rejected_operation_is_recorded_with_the_error():
    client = TestClient(app)
    response = client.post(
        "/api/signals/write", json={"id": "no-such-signal", "value": True, "op_id": "diag-bad"}
    )
    assert response.status_code == 404

    events = client.get("/api/diagnostics/events?limit=50").json()["events"]
    (entry,) = [e for e in events if e.get("op_id") == "diag-bad"]
    assert entry["status"] == "rejected"
    assert entry["errors"]  # причина — в журнале, не только в HTTP-ответе


def test_diagnostics_limit_returns_the_requested_tail():
    client = TestClient(app)
    body = client.get("/api/diagnostics/events?limit=1").json()
    assert len(body["events"]) == 1


# -- интеграция: механизм и реальные OPC UA-узлы (§16) ---------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_INTEGRATION_CONFIGS = [
    {"id": "Application.GVL.Speed", "name": "Скорость", "type": "float", "writable": True, "default": 1.5},
    {"id": "Application.GVL.Alarm", "name": "Авария", "type": "bool", "writable": True, "default": False},
]


def test_apply_changes_reaches_real_opcua_nodes_and_honors_ownership():
    from asyncua import Client

    from app.services.opc_bus import OPCMode, OPCSignalBus

    async def scenario():
        port = _free_port()
        cache = SignalCache(_INTEGRATION_CONFIGS)
        bus = OPCSignalBus(cache, mode=OPCMode.SERVER, server_endpoint=f"opc.tcp://127.0.0.1:{port}")
        registry = OwnershipRegistry()
        bus.set_ownership(registry)
        await bus.connect()
        try:
            result = await bus.apply_changes(
                {"Application.GVL.Speed": 42.0},
                source="manual",
                op_id="e2e-1",
                require_writable=True,
                contract="api",
            )
            assert result["status"] == "applied"

            # независимый клиент читает узел, а не внутреннее состояние
            async with Client(f"opc.tcp://127.0.0.1:{port}") as ext:
                node = ext.get_node("ns=2;s=Application.GVL.Speed")
                assert await node.read_value() == 42.0

            # занятый сигнал manual-писцу закрыт (владение, §9.2)
            registry.acquire(
                Owner(id="scenario:e2e", kind="scenario", title="E2E прогон"),
                ["Application.GVL.Alarm"],
            )
            blocked = await bus.apply_changes(
                {"Application.GVL.Alarm": True},
                source="manual",
                require_writable=True,
                contract="api",
            )
            assert blocked["status"] == "rejected"
            assert blocked["results"]["Application.GVL.Alarm"]["code"] == "owned"
            async with Client(f"opc.tcp://127.0.0.1:{port}") as ext:
                alarm = ext.get_node("ns=2;s=Application.GVL.Alarm")
                assert await alarm.read_value() is False  # значение не тронуто
        finally:
            await bus.close()

    asyncio.run(scenario())


def test_node_write_failure_is_reported_not_masked():
    from app.services.opc_bus import OPCMode, OPCSignalBus

    async def scenario():
        cache = SignalCache(_INTEGRATION_CONFIGS)
        bus = OPCSignalBus(cache, mode=OPCMode.SERVER)

        class _BrokenEmbedded:
            async def write_value(self, signal_id, value):
                return False  # узел недоступен — запись не проходит

        bus._embedded = _BrokenEmbedded()
        result = await bus.apply_changes(
            {"Application.GVL.Speed": 9.0},
            source="manual",
            require_writable=True,
            contract="api",
        )
        assert result["status"] == "failed"
        assert result["results"]["Application.GVL.Speed"]["code"] == "write_failed"
        assert cache.get_value("Application.GVL.Speed") == 1.5  # значение прежнее
        assert cache.get_revision("Application.GVL.Speed") == 0  # подтверждения нет

    asyncio.run(scenario())


# -- аварийный перезапуск: последний валидный снимок (§16 Recovery) --------------


def test_crash_loses_only_writes_after_the_last_snapshot(tmp_path):
    cards = {
        "a": {"value": 1.0, "revision": 1, "source": "manual", "changed_at": "t1"},
        "b": {"value": False, "revision": 0, "source": None, "changed_at": None},
    }
    store = SnapshotStore(
        path=tmp_path / "server_state.json",
        fingerprint="fp",
        signal_ids=["a", "b"],
        read_card=lambda sid: cards[sid],
    )
    assert store.save() is True

    # «падение»: значения изменились, снимок снят не был
    cards["a"] = {"value": 5.0, "revision": 2, "source": "manual", "changed_at": "t2"}

    document, reason = store.load()
    assert reason == "ok"
    assert document["signals"]["a"] == {"v": 1.0, "r": 1, "s": "manual", "t": "t1"}
    assert document["signals"]["b"]["v"] is False


# диагностика ставится в роут при старте приложения — фиксация проводки
def test_app_wiring_uses_one_shared_journal():
    from app.api import diagnostics_routes

    client = TestClient(app)
    client.get("/api/diagnostics/events?limit=1")
    assert diagnostics_routes._journal is _diagnostics
    set_diagnostics_journal(_diagnostics)  # возврат состояния после чтения
