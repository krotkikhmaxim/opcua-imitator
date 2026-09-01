import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

import pytest
from fastapi.testclient import TestClient

from app.config import load_signal_config, settings
from app.main import app
from app.services.signal_cache import SignalCache
from app.services.storage import StateStorage
from app.api.routes import set_services
from app.services.opc_bus import OPCSignalBus, OPCMode

import tempfile

@pytest.fixture()
def client(tmp_path):
    settings.SAVED_STATES_DIR = str(tmp_path / "saved_states")
    cache = SignalCache(load_signal_config())
    bus = OPCSignalBus(cache, mode=OPCMode.IMITATOR)
    storage = StateStorage(settings.SAVED_STATES_DIR)
    set_services(bus, storage)
    return TestClient(app)


def test_list_empty(client):
    r = client.get("/api/states/list")
    assert r.status_code == 200
    assert r.json() == []


def test_save_and_list(client):
    r = client.post("/api/states/save", json={"name": "Режим работы №1", "description": "Тест"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["signal_count"] > 0
    assert body["filename"].endswith(".json")
    assert body["filename"].startswith("Режим_работы")

    states = client.get("/api/states/list").json()
    assert len(states) == 1
    assert states[0]["name"] == "Режим работы №1"


def test_save_empty_name(client):
    r = client.post("/api/states/save", json={"name": "  ", "description": ""})
    assert r.status_code == 400


def test_load_updates_cache(client):
    client.post("/api/states/save", json={"name": "x", "description": ""})
    states = client.get("/api/states/list").json()
    filename = states[0]["filename"]

    r = client.post("/api/states/load", json={"filename": filename})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["loaded_count"] > 0


def test_load_missing(client):
    r = client.post("/api/states/load", json={"filename": "nope.json"})
    assert r.status_code == 404


def test_delete(client):
    client.post("/api/states/save", json={"name": "x", "description": ""})
    states = client.get("/api/states/list").json()
    filename = states[0]["filename"]
    r = client.delete(f"/api/states/delete/{filename}")
    assert r.status_code == 200
    assert client.get("/api/states/list").json() == []


def test_export(client):
    r = client.post("/api/states/export", json={"name": "exp", "description": ""})
    assert r.status_code == 200
    assert "exp.json" in r.headers.get("content-disposition", "")


def test_import(client):
    import json
    data = {
        "name": "imported",
        "description": "test",
        "signals": {"ch1-10a3-sq1": True, "profibus-motor-speed": 42},
    }
    r = client.post(
        "/api/states/import",
        files={"file": ("s.json", json.dumps(data).encode(), "application/json")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["signal_count"] == 2
    assert body["filename"] == "imported.json" or body["filename"].startswith("imported_")


def test_import_invalid(client):
    r = client.post(
        "/api/states/import",
        files={"file": ("s.json", b"not json", "application/json")},
    )
    assert r.status_code == 400


def test_signals_list_has_new_signals(client):
    r = client.get("/api/signals")
    assert r.status_code == 200
    signals = r.json()["signals"]
    assert len(signals) == 101
    sig = signals["ch1-10a3-sq1"]
    assert sig["project_tag"] == (
        "Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ1_SinhLeftTop"
    )
    assert sig["address"] == sig["project_tag"]
    assert sig["type"] == "Boolean"
    assert sig["channel"] in ("Chanel1", "Chanel2")


def test_write_signal(client):
    sig_id = "ch1-10a3-sq1"
    r = client.post("/api/signals/write", json={"id": sig_id, "value": True})
    assert r.status_code == 200
    assert r.json()["value"] is True

    signals = client.get("/api/signals").json()["signals"]
    assert signals[sig_id]["value"] is True


def test_write_int_signal(client):
    sig_id = "ch1-10a3-sq1"
    r = client.post("/api/signals/write", json={"id": sig_id, "value": "1"})
    assert r.status_code == 200
    assert r.json()["value"] is True


def test_write_unknown_signal(client):
    r = client.post("/api/signals/write", json={"id": "nope", "value": 1})
    assert r.status_code == 404


def test_write_invalid_value(client):
    sig_id = "profibus-motor-speed"
    r = client.post("/api/signals/write", json={"id": sig_id, "value": "abc"})
    assert r.status_code == 400


def test_write_int16_range(client):
    sig_id = "profibus-motor-speed"  # Int16
    r = client.post("/api/signals/write", json={"id": sig_id, "value": 40000})
    assert r.status_code == 400


def test_write_uint16_range(client):
    sig_id = "profibus-inv-status-word"  # UInt16
    r = client.post("/api/signals/write", json={"id": sig_id, "value": -1})
    assert r.status_code == 400
    r = client.post("/api/signals/write", json={"id": sig_id, "value": 65535})
    assert r.status_code == 200


def test_write_batch(client):
    a = "ch1-10a3-sq1"
    b = "profibus-motor-speed"
    r = client.post("/api/signals/write_batch", json={"values": {a: True, b: 1200}})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["applied"]) == {a, b}
    assert body["errors"] == []

    signals = client.get("/api/signals").json()["signals"]
    assert signals[a]["value"] is True
    assert signals[b]["value"] == 1200


def test_write_batch_partial_errors(client):
    r = client.post(
        "/api/signals/write_batch",
        json={"values": {"unknown_sig": 1, "profibus-motor-speed": "abc"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == []
    assert len(body["errors"]) == 2
