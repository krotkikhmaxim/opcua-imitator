"""Реестр дочерних экземпляров менеджера (WP-F).

Проверяются: окружение и порты дочернего процесса, каталоги состояния под
``<root>/<user>``, сохранение снимка при останове, чистка простоя по
подменным часам, ёмкость (503-условие) и устойчивость слотов между
рестартами менеджера. Реальный подъём — один интеграционный сценарий на
моде ``imitator`` (без OPC UA-сервера, быстро): подъём → запись → останов
→ снимок → повторный подъём → значение вернулось.
"""

import asyncio
import socket
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.manager.instances import (
    InstanceLimitError,
    InstanceRegistry,
    InstanceRecord,
)


class FakeClock:
    """Подменные монотонные часы: чистка простоя проверяется без ожидания."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeProcess:
    """Двойник Popen: poll() решает, жив ли процесс."""

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self.waited = True
        return 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _registry(tmp_path: Path, *, spawn_enabled: bool = False, **kwargs) -> InstanceRegistry:
    return InstanceRegistry(
        users_root=tmp_path / "users",
        spawn_enabled=spawn_enabled,
        **kwargs,
    )


def test_child_env_and_cmd_carry_user_paths_and_single_role(tmp_path):
    registry = _registry(tmp_path, http_base_port=8101, opc_base_port=4841)
    record = asyncio.run(registry.get_or_start("ivan"))

    assert record.slot == 0
    assert record.http_port == 8101
    assert record.opc_port == 4841
    assert record.username == "imit_ivan"
    assert record.password  # сгенерирован

    env = registry.child_env(record)
    root = (tmp_path / "users" / "ivan").resolve()
    assert env["IMITATOR_ROLE"] == "single"
    assert env["OPC_UA_SERVER_ENDPOINT"] == "opc.tcp://0.0.0.0:4841"
    assert Path(env["SAVED_STATES_DIR"]) == root / "saved_states"
    assert Path(env["SCENARIO_JOURNAL_PATH"]) == root / "scenario_runs" / "journal.jsonl"
    assert Path(env["VERIFICATION_DIR"]) == root / "verifications"
    assert Path(env["PERSIST_SNAPSHOT_PATH"]) == root / "data" / "server_state.json"
    assert Path(env["PLAYBACK_UPLOAD_DIR"]) == root / "playback_uploads"
    assert env["OPC_UA_SERVER_USER"] == "imit_ivan"
    assert env["OPC_UA_SERVER_PASSWORD"] == record.password

    cmd = registry.child_cmd(record)
    assert "app.main:app" in cmd
    assert "127.0.0.1" in cmd and "8101" in cmd
    # Пароль никогда не попадает в командную строку — только окружение.
    assert record.password not in " ".join(cmd)
    # Каталоги состояния подготовлены к подъёму.
    assert (root / "saved_states").is_dir()
    assert (root / "data").is_dir()
    asyncio.run(registry.aclose())


def test_capacity_limit_raises_instance_limit(tmp_path):
    registry = _registry(tmp_path, max_instances=1)
    asyncio.run(registry.get_or_start("anna"))
    with pytest.raises(InstanceLimitError):
        asyncio.run(registry.get_or_start("ivan"))
    # Пользователь со слотом живёт дальше: повторный запрос — не ошибка.
    record = asyncio.run(registry.get_or_start("anna"))
    assert record.slot == 0
    asyncio.run(registry.aclose())


def test_slots_stable_across_registry_restarts(tmp_path):
    first = _registry(tmp_path)
    record = asyncio.run(first.get_or_start("ivan"))
    second_record = asyncio.run(first.get_or_start("maria"))
    asyncio.run(first.aclose())

    second = _registry(tmp_path)
    restored = asyncio.run(second.get_or_start("ivan"))
    assert restored.slot == record.slot
    assert restored.http_port == record.http_port
    assert restored.opc_port == record.opc_port
    assert restored.username == record.username
    assert restored.password == record.password  # станция не перенастраивается
    # Новый пользователь получает следующий свободный слот.
    assert second_record.slot == 1
    assert second.record_of("maria").opc_port == 4842
    asyncio.run(second.aclose())


def test_reaper_stops_idle_instance(tmp_path):
    clock = FakeClock()
    registry = _registry(tmp_path, idle_timeout_s=1800.0, clock=clock)
    record = InstanceRecord(
        user="ivan",
        slot=0,
        http_port=8101,
        opc_port=4841,
        username="imit_ivan",
        password="pw",
        created_at="2026-10-21T00:00:00+00:00",
        updated_at="2026-10-21T00:00:00+00:00",
        process=FakeProcess(alive=True),
        desired="running",
        last_activity=clock(),
    )
    registry._records["ivan"] = record

    # Не простоял лимит — жив.
    clock.now += 60
    asyncio.run(registry.reap())
    assert record.alive

    # Простоял сверх IMITATOR_IDLE_TIMEOUT_S — остановлен (SIGTERM ушёл,
    # штатный останов дочернего процесса сохранит его снимок).
    fake_process = record.process
    clock.now += 1900
    asyncio.run(registry.reap())
    assert not record.alive
    assert fake_process.terminated
    assert record.state == "stopped"
    asyncio.run(registry.aclose())


def test_reaper_restarts_crashed_instance(tmp_path):
    clock = FakeClock()
    registry = _registry(tmp_path, clock=clock)
    dead = FakeProcess(alive=False)
    record = InstanceRecord(
        user="ivan",
        slot=0,
        http_port=8101,
        opc_port=4841,
        username="imit_ivan",
        password="pw",
        created_at="2026-10-21T00:00:00+00:00",
        updated_at="2026-10-21T00:00:00+00:00",
        process=dead,
        desired="running",
        last_activity=clock(),
    )
    registry._records["ivan"] = record

    # Падение при свежей активности: перезапуск (spawn в тесте отключён —
    # желание держать экземпляр поднятым сохраняется).
    asyncio.run(registry.reap())
    assert record.desired == "running"
    assert record.process is None  # процесс заменён при перезапуске

    # Падение при простое сверх лимита: считается остановленным.
    record.process = FakeProcess(alive=False)
    record.last_activity = clock.now - 3600
    asyncio.run(registry.reap())
    assert record.desired == "stopped"
    asyncio.run(registry.aclose())


def test_reaper_loop_fires_on_schedule(tmp_path):
    clock = FakeClock()
    registry = _registry(tmp_path, clock=clock, reaper_interval_s=0.01, idle_timeout_s=10.0)
    record = InstanceRecord(
        user="ivan",
        slot=0,
        http_port=8101,
        opc_port=4841,
        username="imit_ivan",
        password="pw",
        created_at="2026-10-21T00:00:00+00:00",
        updated_at="2026-10-21T00:00:00+00:00",
        process=FakeProcess(alive=True),
        desired="running",
        last_activity=clock(),
    )
    registry._records["ivan"] = record

    async def scenario() -> None:
        registry.start_reaper()
        try:
            for _ in range(400):
                await asyncio.sleep(0.005)
                clock.now += 1
                if record.state == "stopped":
                    return
            pytest.fail("чистка простоя не сработала за отведённое время")
        finally:
            await registry.stop_reaper()

    asyncio.run(scenario())
    asyncio.run(registry.aclose())


def test_spawn_saves_snapshot_on_stop_and_restores_on_restart(tmp_path, monkeypatch):
    """Полный контур личного экземпляра: подъём → запись → останов → снимок.

    Повторный подъём на тех же портах восстанавливает записанное значение —
    «настройки вернулись после аутентификации» без отдельной процедуры.
    """
    monkeypatch.setenv("OPC_UA_MODE", "imitator")  # дочернему процессу: без OPC UA-сервера
    registry = InstanceRegistry(
        users_root=tmp_path / "users",
        http_base_port=_free_port(),
        opc_base_port=_free_port(),
        startup_timeout_s=30.0,
        spawn_enabled=True,
    )
    snapshot_path = tmp_path / "users" / "ivan" / "data" / "server_state.json"

    async def scenario() -> None:
        record = await registry.get_or_start("ivan")
        assert record.state == "running"
        base = f"http://127.0.0.1:{record.http_port}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base}/api/signals/write", json={"id": "ch1-10a3-sq1", "value": True}
            )
            assert response.status_code == 200, response.text
        await registry.stop("ivan")
        assert snapshot_path.exists()  # штатный останов сохранил снимок

        restarted = await registry.get_or_start("ivan")
        assert (restarted.http_port, restarted.opc_port) == (
            record.http_port,
            record.opc_port,
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = (await client.get(f"{base}/api/signals")).json()
        assert payload["signals"]["ch1-10a3-sq1"]["value"] is True
        await registry.stop_all()
        await registry.aclose()

    asyncio.run(scenario())
