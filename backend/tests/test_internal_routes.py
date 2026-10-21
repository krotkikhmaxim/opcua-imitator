"""Внутренний API менеджера (WP-F) — контракт для станции.

Схема ответа ``GET /internal/instances`` — ``imitator.instances.v1`` —
заморожена: станция строится на ней. Токен обязателен, сравнение в
постоянном времени, пустой IMITATOR_INTERNAL_TOKEN закрывает API.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.manager.instances import InstanceRegistry
from app.manager.routes import INSTANCES_SCHEMA, install_manager

ENTRY_KEYS = {
    "user",
    "http_port",
    "opc_port",
    "state",
    "endpoint",
    "username",
    "password",
    "updated_at",
}

TOKEN = "internal-sekret"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("IMITATOR_INTERNAL_TOKEN", TOKEN)
    client_app = FastAPI()
    registry = InstanceRegistry(
        users_root=tmp_path / "users",
        http_base_port=8101,
        opc_base_port=4841,
        spawn_enabled=False,
    )
    install_manager(client_app, registry=registry)
    return client_app, registry


def test_token_required_and_wrong_rejected(app):
    client_app, _ = app
    with TestClient(client_app) as client:
        missing = client.get("/internal/instances")
        assert missing.status_code == 403
        assert missing.json() == {"code": "internal_forbidden"}

        wrong = client.get(
            "/internal/instances", headers={"X-Internal-Token": "not-it"}
        )
        assert wrong.status_code == 403
        assert wrong.json() == {"code": "internal_forbidden"}

        # Длинный неверный токен — тот же отказ (сравнение не по длине).
        long_wrong = client.get(
            "/internal/instances", headers={"X-Internal-Token": "x" * 4096}
        )
        assert long_wrong.status_code == 403
        assert long_wrong.json() == {"code": "internal_forbidden"}

        stop = client.post("/internal/instances/ivan/stop")
        assert stop.status_code == 403
        assert stop.json() == {"code": "internal_forbidden"}


def test_empty_token_closes_api(app, monkeypatch):
    client_app, _ = app
    monkeypatch.setenv("IMITATOR_INTERNAL_TOKEN", "")
    with TestClient(client_app) as client:
        response = client.get("/internal/instances")
        assert response.status_code == 403
        assert response.json() == {"code": "internal_forbidden"}


def test_instances_schema_is_frozen(app, monkeypatch):
    """Точная форма imitator.instances.v1: дефолтная запись + слоты пользователей."""
    client_app, registry = app
    monkeypatch.delenv("OPC_UA_SERVER_ENDPOINT", raising=False)
    record = asyncio.run(registry.get_or_start("ivan"))

    with TestClient(client_app) as client:
        response = client.get("/internal/instances", headers={"X-Internal-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()

    # Верхний уровень: ровно два ключа, схема — ровно эта строка.
    assert set(body) == {"schema", "instances"}
    assert body["schema"] == "imitator.instances.v1"
    assert INSTANCES_SCHEMA == "imitator.instances.v1"

    entries = body["instances"]
    assert [entry["user"] for entry in entries] == ["default", "ivan"]

    default, ivan = entries
    for entry in entries:
        assert set(entry) == ENTRY_KEYS

    # Дефолтная полоса: реальные порты и анонимность (пустая пара).
    assert default == {
        "user": "default",
        "http_port": 8000,
        "opc_port": 4840,
        "state": "running",
        "endpoint": "opc.tcp://opcua-imitator:4840",
        "username": "",
        "password": "",
        "updated_at": default["updated_at"],  # iso8601, проверяется ниже
    }
    assert default["updated_at"].endswith("+00:00")

    # Личный экземпляр: слот 0 → 8101/4841, пара менеджера.
    assert ivan["user"] == "ivan"
    assert ivan["http_port"] == 8101
    assert ivan["opc_port"] == 4841
    assert ivan["state"] == "stopped"
    assert ivan["endpoint"] == "opc.tcp://opcua-imitator:4841"
    assert ivan["username"] == "imit_ivan"
    assert ivan["password"] == record.password
    assert ivan["password"] != ""
    assert ivan["updated_at"].endswith("+00:00")


def test_stop_endpoint_reports_state(app):
    client_app, registry = app
    asyncio.run(registry.get_or_start("ivan"))
    with TestClient(client_app) as client:
        headers = {"X-Internal-Token": TOKEN}
        unknown = client.post("/internal/instances/nobody/stop", headers=headers)
        assert unknown.status_code == 200
        assert unknown.json() == {"status": "not_running", "user": "nobody"}

        stopped = client.post("/internal/instances/ivan/stop", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json() == {"status": "not_running", "user": "ivan"}

