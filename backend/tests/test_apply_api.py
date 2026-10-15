"""API-контракт механизма применения (ТЗ #5, шаг 4): ревизии и op_id наружу."""

from __future__ import annotations

import os

os.environ["OPC_UA_MODE"] = "imitator"  # до импорта app.main (конвенция сьюта)

import pytest
from fastapi.testclient import TestClient

from app.main import app

BOOL_SIG = "ch1-10a3-sq1"


@pytest.fixture()
def client():
    # Без контекст-менеджера: lifespan здесь не нужен (он поднимает
    # встроенный сервер на 4840 — порт живого бэкенда).
    return TestClient(app)


def test_signals_list_carries_state_card(client):
    client.post("/api/signals/write", json={"id": BOOL_SIG, "value": True})
    sig = client.get("/api/signals").json()["signals"][BOOL_SIG]
    assert sig["revision"] >= 1
    assert sig["source"] == "manual"
    assert sig["changed_at"] is not None


def test_write_response_carries_revision_and_op_id(client):
    r = client.post(
        "/api/signals/write",
        json={"id": BOOL_SIG, "value": True, "op_id": "api-op-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["op_id"] == "api-op-1"
    assert body["revision"] >= 1
    assert body["source"] == "manual"

    # Повтор того же op_id — тот же ответ без повторного применения.
    again = client.post(
        "/api/signals/write",
        json={"id": BOOL_SIG, "value": True, "op_id": "api-op-1"},
    )
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["revision"] == body["revision"]


def test_same_op_id_other_body_conflict(client):
    client.post("/api/signals/write", json={"id": BOOL_SIG, "value": True, "op_id": "api-op-2"})
    r = client.post("/api/signals/write", json={"id": BOOL_SIG, "value": False, "op_id": "api-op-2"})
    assert r.status_code == 409


def test_expected_revision_conflict_409(client):
    sig = client.get("/api/signals").json()["signals"][BOOL_SIG]
    stale = sig["revision"]
    r = client.post(
        "/api/signals/write",
        json={"id": BOOL_SIG, "value": True, "expected_revision": stale},
    )  # первая запись проходит и двигает ревизию
    assert r.status_code == 200
    r = client.post(
        "/api/signals/write",
        json={"id": BOOL_SIG, "value": False, "expected_revision": stale},
    )
    assert r.status_code == 409
    assert "ревизия" in r.json()["detail"]


def test_batch_response_carries_results_and_op_id(client):
    r = client.post(
        "/api/signals/write_batch",
        json={"values": {BOOL_SIG: True, "profibus-motor-speed": 1500},
              "op_id": "api-batch-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "applied"
    assert body["op_id"] == "api-batch-1"
    assert body["results"][BOOL_SIG]["status"] == "applied"
    assert body["results"]["profibus-motor-speed"]["value"] == 1500

    replay = client.post(
        "/api/signals/write_batch",
        json={"values": {BOOL_SIG: True, "profibus-motor-speed": 1500},
              "op_id": "api-batch-1"},
    )
    assert replay.json()["replayed"] is True
