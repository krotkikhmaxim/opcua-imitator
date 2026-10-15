"""Снимки и восстановление состояния сервера (ТЗ #5, шаг 8 — §13).

Покрывает: атомарный файл снимка (схема, отпечаток реестра, карточки
{v, r, s, t}, отметка завершённости), отказ целиком на повреждённом или
чужом снимке, восстановление карточек без роста ревизий, запись значений
во встроенный сервер при старте, сохранение на границах применения
(ручная запись, пакет, сброс, загрузка состояния), на переходах сценария
и при штатном останове, периодический снимок только во время прогона.
"""

import asyncio
import json
import os

os.environ.setdefault("OPC_UA_MODE", "imitator")  # до импорта app.main

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.api.routes import _holder
from app.config import load_signal_config
from app.main import _cache, _snapshot_store, app
from app.services.ownership import OwnershipRegistry
from app.services.persistence import SNAPSHOT_SCHEMA, SnapshotStore, registry_fingerprint
from app.services.scenario import ScenarioEngine
from app.services.signal_cache import SignalCache
from app.services.signal_types import default_for

CONFIG = load_signal_config()
IDS = [cfg["id"] for cfg in CONFIG]
FP = registry_fingerprint(CONFIG)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


# -- хранилище: сохранение и отказ целиком (§13.1, §13.3) ---------------------


def _cards():
    return {
        "a": {"value": 3.5, "revision": 7, "source": "manual", "changed_at": "2026-10-15T10:00:00+00:00"},
        "b": {"value": True, "revision": 2, "source": "scenario:run-1", "changed_at": "2026-10-15T10:00:01+00:00"},
    }


def _store(tmp_path, fingerprint=FP, **kw):
    return SnapshotStore(
        path=tmp_path / "state" / "server_state.json",
        fingerprint=fingerprint,
        signal_ids=["a", "b"],
        read_card=lambda sid: _cards()[sid],
        **kw,
    )


def test_save_then_load_round_trip_keeps_cards_and_meta(tmp_path):
    store = _store(tmp_path)
    assert store.save() is True

    document, reason = store.load()
    assert reason == "ok"
    assert document["schema"] == SNAPSHOT_SCHEMA
    assert document["registry"] == FP
    assert document["complete"] is True
    assert datetime.fromisoformat(document["created_at"])
    assert document["signals"] == {
        "a": {"v": 3.5, "r": 7, "s": "manual", "t": "2026-10-15T10:00:00+00:00"},
        "b": {"v": True, "r": 2, "s": "scenario:run-1", "t": "2026-10-15T10:00:01+00:00"},
    }
    # атомарная замена: временного файла после записи не остаётся
    assert not (tmp_path / "state" / "server_state.json.tmp").exists()


def test_missing_snapshot_is_a_first_start(tmp_path):
    store = _store(tmp_path)
    document, reason = store.load()
    assert document is None and "нет" in reason


def test_corrupt_snapshot_is_rejected_wholly(tmp_path):
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{не json", encoding="utf-8")
    document, reason = store.load()
    assert document is None and "повреждён" in reason


def test_foreign_schema_is_rejected(tmp_path):
    store = _store(tmp_path)
    store.save()
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    doc["schema"] = "someone-else.v9"
    store.path.write_text(json.dumps(doc), encoding="utf-8")

    document, reason = store.load()
    assert document is None and "несовместимая схема" in reason


def test_snapshot_of_another_registry_is_rejected(tmp_path):
    store = _store(tmp_path)
    store.save()
    other = _store(tmp_path, fingerprint="deadbeefcafe")
    document, reason = other.load()
    assert document is None and "другого реестра" in reason
    assert "deadbeefcafe" in reason and FP in reason


def test_incomplete_snapshot_is_rejected(tmp_path):
    store = _store(tmp_path)
    store.save()
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    doc["complete"] = False
    store.path.write_text(json.dumps(doc), encoding="utf-8")

    document, reason = store.load()
    assert document is None and "неполный" in reason


def test_periodic_loop_saves_only_while_active(tmp_path):
    store = _store(tmp_path, interval_s=0.02, active=lambda: _active[0])
    _active = [True]

    async def run():
        store.start()
        await asyncio.sleep(0.07)
        assert store.path.exists()  # активный период — снимок появился

        store.path.unlink()
        _active[0] = False
        await asyncio.sleep(0.07)
        assert not store.path.exists()  # спокойное состояние — не пишем
        await store.stop()

    asyncio.run(run())


def test_zero_interval_disables_the_periodic_loop(tmp_path):
    store = _store(tmp_path, interval_s=0, active=lambda: True)
    assert store.periodic_disabled

    async def run():
        store.start()
        assert not store.running  # задач нет — периодический снимок выключен
        await store.stop()

    asyncio.run(run())


# -- восстановление карточек и значений (§13.3) --------------------------------


def test_restore_moves_cards_without_revision_growth():
    sid, other = CONFIG[0]["id"], CONFIG[1]["id"]
    cache = SignalCache(CONFIG)
    restored = cache.restore({
        sid: {"v": 3.5, "r": 7, "s": "manual", "t": "2026-10-15T10:00:00+00:00"},
        "zzz": {"v": 1.0, "r": 1, "s": "manual", "t": "2026-10-15T10:00:00+00:00"},
    })
    assert restored == 1  # неизвестный сигнал пропущен

    state = cache.get_state(sid)
    assert state.value == 3.5
    assert state.revision == 7  # восстановление — не команда применения
    assert state.source == "manual"
    assert state.changed_at == "2026-10-15T10:00:00+00:00"

    assert cache.confirm(sid, 4.0) == 8  # следующая команда растёт от снятой ревизии
    assert cache.get_revision(other) == 0  # сосед остался при начальном значении


def test_bus_restore_values_writes_the_embedded_server():
    from app.services.opc_bus import OPCMode, OPCSignalBus

    cache = SignalCache(CONFIG)
    bus = OPCSignalBus(cache, mode=OPCMode.SERVER)

    class _FakeEmbedded:
        def __init__(self):
            self.writes = []

        async def write_value(self, signal_id, value):
            self.writes.append((signal_id, value))
            return True

    fake = _FakeEmbedded()
    bus._embedded = fake

    applied = asyncio.run(bus.restore_values({"a": 3.5, "b": True}))
    assert applied == 2
    assert fake.writes == [("a", 3.5), ("b", True)]


def test_bus_restore_values_is_a_noop_outside_server_mode():
    from app.services.opc_bus import OPCMode, OPCSignalBus

    cache = SignalCache(CONFIG)
    sid = CONFIG[0]["id"]
    bus = OPCSignalBus(cache, mode=OPCMode.IMITATOR)
    assert asyncio.run(bus.restore_values({sid: 3.5})) == 0
    assert cache.get_value(sid) != 3.5  # кэш restore_values не трогает


# -- переходы сценария сохраняют снимок (§13.2) ---------------------------------


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


def _hooked_engine(tmp_path, phases, gate=None):
    def writer_factory(run_id):
        async def writer(signal_id, value, when):
            return True

        return writer

    kwargs = {}
    if gate is not None:
        kwargs = {"sleep": gate.sleep, "monotonic": gate.monotonic}
    else:
        clock = _VirtualTime()
        kwargs = {"sleep": clock.sleep, "monotonic": clock.monotonic}
    return ScenarioEngine(
        writer_factory,
        CONFIG,
        simulated=True,
        ownership=OwnershipRegistry(),
        journal_path=tmp_path / "journal.jsonl",
        on_transition=lambda phase: phases.append(phase),
        **kwargs,
    )


def test_engine_snapshot_hook_fires_on_finish(tmp_path):
    phases = []
    engine = _hooked_engine(tmp_path, phases)

    async def run():
        status = await engine.start(kinds=["normal_stop"], seed=1, once=True)
        await engine._runs[status["run_id"]].task

    asyncio.run(run())
    assert phases == ["finished"]


def test_engine_snapshot_hook_fires_on_pause_and_stop(tmp_path):
    phases = []
    gate = _OneShotGate()
    gate.armed = True
    engine = _hooked_engine(tmp_path, phases, gate=gate)

    async def run():
        status = await engine.start(kinds=["normal_stop"], seed=2, once=True)
        await asyncio.sleep(0)
        assert engine.running
        await engine.pause(status["run_id"])
        assert "paused" in phases
        await engine.stop()
        assert "stopped" in phases

    asyncio.run(run())


def test_engine_survives_a_failing_snapshot_hook(tmp_path):
    def broken(_phase):
        raise RuntimeError("disk full")

    def writer_factory(run_id):
        async def writer(signal_id, value, when):
            return True

        return writer

    clock = _VirtualTime()
    engine = ScenarioEngine(
        writer_factory,
        CONFIG,
        simulated=True,
        ownership=OwnershipRegistry(),
        journal_path=tmp_path / "journal.jsonl",
        on_transition=broken,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    async def run():
        status = await engine.start(kinds=["normal_stop"], seed=3, once=True)
        await engine._runs[status["run_id"]].task

    asyncio.run(run())
    assert engine.status()["phase"] == "finished"  # сбой снимка не роняет прогон


# -- границы применения через API сохраняют снимок (§13.2) ----------------------


def _writable_bool():
    return next(
        cfg for cfg in CONFIG if cfg.get("writable") and (cfg.get("type") or "Boolean") == "Boolean"
    )


def _patch_snapshot_path(monkeypatch, tmp_path):
    path = tmp_path / "server_state.json"
    monkeypatch.setattr(_snapshot_store, "_path", path)
    return path


def _load_snapshot(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_updates_the_snapshot_file(tmp_path, monkeypatch):
    path = _patch_snapshot_path(monkeypatch, tmp_path)
    cfg = _writable_bool()
    client = TestClient(app)

    response = client.post("/api/signals/write", json={"id": cfg["id"], "value": True})
    assert response.status_code == 200

    document = _load_snapshot(path)
    assert document["schema"] == SNAPSHOT_SCHEMA
    assert document["registry"] == FP
    card = document["signals"][cfg["id"]]
    assert card["v"] is True
    assert card["s"] == "manual"
    assert card["r"] >= 1


def test_write_batch_and_reset_update_the_snapshot_file(tmp_path, monkeypatch):
    path = _patch_snapshot_path(monkeypatch, tmp_path)
    cfg = _writable_bool()
    client = TestClient(app)

    response = client.post(
        "/api/signals/write_batch",
        json={"values": {cfg["id"]: True}, "op_id": "snap-batch-1"},
    )
    assert response.status_code == 200
    assert _load_snapshot(path)["signals"][cfg["id"]]["s"] == "manual_batch"

    response = client.post("/api/signals/reset", json={"signal_ids": [cfg["id"]]})
    assert response.status_code == 200
    card = _load_snapshot(path)["signals"][cfg["id"]]
    assert card["v"] == default_for(cfg.get("type") or "Boolean")
    assert card["s"] == "reset"


def test_state_load_updates_the_snapshot_file(tmp_path, monkeypatch):
    path = _patch_snapshot_path(monkeypatch, tmp_path)
    cfg = _writable_bool()

    class _FakeStorage:
        def read(self, filename):
            return {"signals": {cfg["id"]: True}}

    monkeypatch.setattr(_holder, "storage", _FakeStorage())
    client = TestClient(app)

    response = client.post("/api/states/load", json={"filename": "saved.json"})
    assert response.status_code == 200

    card = _load_snapshot(path)["signals"][cfg["id"]]
    assert card["v"] is True
    assert card["s"] == "state_load"


def test_startup_restores_and_shutdown_saves(tmp_path, monkeypatch):
    path = _patch_snapshot_path(monkeypatch, tmp_path)
    cfg = _writable_bool()
    sid = cfg["id"]
    document = {
        "schema": SNAPSHOT_SCHEMA,
        "registry": FP,
        "created_at": NOW.isoformat(),
        "signals": {
            **{other: {"v": None, "r": 0, "s": None, "t": None} for other in IDS if other != sid},
            sid: {"v": True, "r": 42, "s": "manual", "t": "2026-10-15T10:00:00+00:00"},
        },
        "complete": True,
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    with TestClient(app) as client:
        signals = client.get("/api/signals").json()["signals"]
        assert signals[sid]["value"] is True  # значение восстановлено
        assert signals[sid]["revision"] == 42  # ревизия перенесена как есть
        assert signals[sid]["source"] == "manual"
        assert signals[sid]["owner"] is None  # владение после рестарта пусто
        # сценарии не возобновляются: движок стартует без прогонов
        from app.api.scenario_routes import get_scenario_engine

        assert get_scenario_engine().status()["running"] is False

        path.unlink()  # сотрём — штатный останов обязан снять снимок заново

    assert path.exists()
    saved = _load_snapshot(path)
    assert saved["complete"] is True
    assert saved["signals"][sid]["v"] is True  # shutdown-снимок держит значение
