"""Сброс значений через общий механизм применения (ТЗ #5, шаг 6 — §8).

Сброс — та же команда, что ручная запись: пакетная проверка, владение,
дедупликация op_id; начальные значения берутся из реестра (``default``).
Таймлайн не затрагивается.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

from fastapi.testclient import TestClient

from app.main import _cache, _ownership, app
from app.services.ownership import Owner

client = TestClient(app)


def _writable_ids(n: int) -> list[str]:
    ids = [cfg["id"] for cfg in _cache.config if cfg.get("writable")]
    assert len(ids) >= n
    return ids[:n]


def _cfg(signal_id: str) -> dict:
    return next(cfg for cfg in _cache.config if cfg["id"] == signal_id)


def _set_and_check(signal_id: str) -> None:
    """Уводим сигнал от начального значения и убеждаемся, что он применился."""
    cfg = _cfg(signal_id)
    non_default = not cfg.get("default")
    assert client.post(
        "/api/signals/write", json={"id": signal_id, "value": non_default}
    ).status_code == 200
    assert client.get("/api/signals").json()["signals"][signal_id]["value"] is non_default


def test_reset_without_list_restores_defaults_of_whole_writable_set():
    first, second = _writable_ids(2)
    _set_and_check(first)
    _set_and_check(second)

    response = client.post("/api/signals/reset", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applied"
    # Весь записываемый набор реестра, не только тронутые вручную.
    writable = [cfg["id"] for cfg in _cache.config if cfg.get("writable")]
    assert sorted(body["applied"]) == sorted(writable)

    signals = client.get("/api/signals").json()["signals"]
    for signal_id in (first, second):
        cfg = _cfg(signal_id)
        assert signals[signal_id]["value"] == cfg.get("default")
        # Подтверждение — источник «reset», ревизия выросла от записи.
        assert signals[signal_id]["source"] == "reset"
        assert signals[signal_id]["revision"] >= 1


def test_reset_selected_set_touches_only_listed_signals():
    first, second = _writable_ids(2)
    _set_and_check(first)
    _set_and_check(second)

    body = client.post(
        "/api/signals/reset", json={"signal_ids": [first]}
    ).json()
    assert body["status"] == "applied"
    assert body["applied"] == [first]

    signals = client.get("/api/signals").json()["signals"]
    assert signals[first]["value"] == _cfg(first).get("default")
    assert signals[first]["source"] == "reset"
    # Сосед по набору сброс не затронул.
    assert signals[second]["source"] != "reset"


def test_reset_unknown_id_is_honest_404():
    response = client.post(
        "/api/signals/reset", json={"signal_ids": ["no-such-signal"]}
    )
    assert response.status_code == 404
    assert "no-such-signal" in response.json()["detail"]


def test_reset_of_owned_signal_rejects_the_whole_package():
    """§8: те же правила владения, что у ручной записи — пакет целиком."""
    held, free = _writable_ids(2)
    owner = Owner(id="scenario:reset01", kind="scenario", title="Сценарий-сброс")
    assert _ownership.acquire(owner, [held]) == []
    try:
        body = client.post(
            "/api/signals/reset", json={"signal_ids": [held, free]}
        ).json()
        assert body["status"] == "rejected"
        assert body["results"][held]["code"] == "owned"
        assert body["results"][free]["code"] == "package_rejected"
        # Ничего не сброшено: свободный сигнал остался в своём значении.
        assert body["applied"] == []
    finally:
        _ownership.release(owner.id)


def test_reset_same_op_id_replays_without_applying():
    first, = _writable_ids(1)
    _set_and_check(first)

    first_body = client.post(
        "/api/signals/reset", json={"signal_ids": [first], "op_id": "reset-dup-1"}
    ).json()
    assert first_body["status"] == "applied"
    revision = client.get("/api/signals").json()["signals"][first]["revision"]

    second_body = client.post(
        "/api/signals/reset", json={"signal_ids": [first], "op_id": "reset-dup-1"}
    ).json()
    assert second_body.get("replayed") is True
    assert second_body["status"] == "applied"
    # Повтор не выполнялся: ревизия не выросла.
    assert (
        client.get("/api/signals").json()["signals"][first]["revision"] == revision
    )


def test_manual_batch_with_expected_revision_conflicts_on_stale_snapshot():
    """§8: устаревший снимок отклоняет пакет целиком — незаметной перезаписи нет."""
    first, = _writable_ids(1)
    stale_revision = client.get("/api/signals").json()["signals"][first]["revision"]

    # Чужая запись между чтением и применением уводит ревизию.
    assert client.post(
        "/api/signals/write", json={"id": first, "value": True}
    ).status_code == 200

    body = client.post(
        "/api/signals/write_batch",
        json={
            "values": {first: False},
            "expected_revisions": {first: stale_revision},
        },
    ).json()
    assert body["status"] == "rejected"
    assert body["results"][first]["code"] == "revision_conflict"
    assert client.get("/api/signals").json()["signals"][first]["value"] is True


def test_manual_batch_with_current_revision_applies():
    first, = _writable_ids(1)
    current = client.get("/api/signals").json()["signals"][first]["revision"]
    body = client.post(
        "/api/signals/write_batch",
        json={
            "values": {first: True},
            "expected_revisions": {first: current},
        },
    ).json()
    assert body["status"] == "applied"
    assert body["results"][first]["value"] is True
