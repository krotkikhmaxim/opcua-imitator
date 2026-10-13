"""Сверка с журналом правды: вердикты причин, состояние, покрытие, маршрут.

Ядро (``build_report``) проверяется на ручных словарях: каждая категория
вердикта причины, отделение присутствия точки от значения, наблюдение за
покрытием без суждения. Маршрут — на моке станции: снимок собирается из
базиса модели, отчёт пишется во временный каталог и читается списком.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.services.scenario.hoist import BASELINE
from app.services.verification import build_report, values_equal


def _point(signal_id, *, value=None, presence="present", tag=None, received_at=None):
    return {
        "point_id": f"p-{signal_id}",
        "tag": tag or f"tag-{signal_id}",
        "symbol_id": None,
        "presence": presence,
        "value": value,
        "value_type": "boolean" if isinstance(value, bool) else None,
        "quality": "good" if presence == "present" else None,
        "status_code": "0",
        "source_timestamp": "2026-03-23T15:00:00+00:00" if presence == "present" else None,
        "server_timestamp": None,
        # Доставка по умолчанию свежая (за секунду до captured_at снапшота):
        # гейт свежести меряет received_at, а не метку источника.
        "received_at": received_at or "2026-03-23T15:04:59+00:00",
        "connection_id": "plc-a",
        "node_id": f"node-{signal_id}",
        "binding_id": signal_id,
    }


def _entry(**overrides):
    entry = {
        "run_id": "run-1",
        "seed": 7,
        "episode": 1,
        "kind": "estop_console",
        "title": "Аварийный стоп с пульта ПМ",
        "fault": True,
        "causes": [
            {"signal_id": "k1", "name": "Стоп", "project_tag": "tag-k1", "before": True, "after": True}
        ],
        "cause_at": "2026-03-23T15:00:01+00:00",
        "stopped_at": "2026-03-23T15:00:05+00:00",
        "recovered_at": "2026-03-23T15:00:09+00:00",
        "cause_visible_after_stop": True,
        "expected_answer": "…",
    }
    entry.update(overrides)
    return entry


# -- значения ----------------------------------------------------------------


def test_values_equal_is_strict_on_bools_and_tolerant_on_numbers():
    assert values_equal(True, True)
    assert not values_equal(True, 1)  # bool против числа — расхождение типов
    assert values_equal(1500, 1500.0)
    assert values_equal(0.1 + 0.2, 0.3)
    assert not values_equal(None, False)
    assert values_equal(None, None)
    assert not values_equal("1", 1)


# -- вердикты причин ---------------------------------------------------------


def test_cause_verdict_matrix():
    snapshot = {
        "snapshot_id": "s",
        "points": [
            _point("k1", value=True),
            _point("k3", value=None, presence="missing"),
        ],
    }
    causes = [
        {"signal_id": "k1", "after": True, "project_tag": "tag-k1"},
        {"signal_id": "k1", "after": False, "project_tag": "tag-k1"},
        {"signal_id": "zz", "after": True, "project_tag": "tag-zz"},
        {"signal_id": "k3", "after": True, "project_tag": "tag-k3"},
    ]
    report = build_report(_entry(causes=causes), snapshot, {})

    verdicts = [v["verdict"] for v in report["cause_check"]["verdicts"]]
    assert verdicts == ["match", "mismatch", "not_bound", "absent"]
    assert report["cause_check"]["counts"] == {
        "match": 1,
        "mismatch": 1,
        "not_bound": 1,
        "absent": 1,
        "history_only": 0,
    }


def test_history_only_cause_is_never_compared():
    snapshot = {"snapshot_id": "s", "points": [_point("k1", value=False)]}
    report = build_report(
        _entry(cause_visible_after_stop=False),
        snapshot,
        {},
    )
    assert report["cause_check"]["verdicts"][0]["verdict"] == "history_only"
    assert report["verdict"] == "pass"  # невидимость причины — не расхождение


# -- состояние ---------------------------------------------------------------


def test_state_check_keeps_presence_apart_from_value():
    snapshot = {
        "snapshot_id": "s",
        "points": [
            _point("k1", value=True),
            _point("k2", value=False),  # ожидаем True — расхождение значения
            _point("k3", value=None, presence="bad_quality"),
        ],
    }
    state = build_report(_entry(), snapshot, {"k1": True, "k2": True, "k3": False, "k4": False})[
        "state_check"
    ]
    assert state["total"] == 4
    assert state["present"] == 2  # k3 есть, но не present — в missing
    assert state["missing"] == 2
    assert state["value_matched"] == 1
    assert state["value_mismatched"] == 1
    assert state["mismatches"] == [
        {
            "signal_id": "k2",
            "expected": True,
            "actual": False,
            "source_timestamp": "2026-03-23T15:00:00+00:00",
        }
    ]


# -- итог и покрытие ---------------------------------------------------------


def test_verdict_fail_partial_pass():
    def snapshot_with(**overrides):
        points = overrides.pop("points")
        return {"snapshot_id": "s", "points": points}

    failing = build_report(
        _entry(), snapshot_with(points=[_point("k1", value=False)]), {"k1": True}
    )
    assert failing["verdict"] == "fail"

    partial = build_report(
        _entry(causes=[{"signal_id": "zz", "after": True, "project_tag": "tag-zz"}]),
        snapshot_with(points=[_point("k1", value=True)]),
        {"k1": True},
    )
    assert partial["verdict"] == "partial"

    passing = build_report(
        _entry(), snapshot_with(points=[_point("k1", value=True)]), {"k1": True}
    )
    assert passing["verdict"] == "pass"


def test_outputs_is_observation_only():
    assert build_report(_entry(), {"snapshot_id": "s", "points": []}, {})["outputs"] is None

    results = {
        "outputs_total": 350,
        "outputs_computed": 136,
        "refusal": None,
        "composition": {
            "by_status": {"calculated": 136, "missing_initial_state": 214},
            "missing_initial_state": {"no_writer": 214},
            "unsupported_reasons": {},
        },
    }
    outputs = build_report(_entry(), {"snapshot_id": "s", "points": []}, {}, results)[
        "outputs"
    ]
    assert outputs["total"] == 350
    assert outputs["computed"] == 136
    assert outputs["by_status"] == {"calculated": 136, "missing_initial_state": 214}
    assert outputs["reason_breakdown"] == {"missing_initial_state.no_writer": 214}
    assert outputs["coverage"] == round(136 / 350, 4)
    # отказ расчёта проходит в наблюдение как есть и вердикт не трогает
    refused = build_report(
        _entry(), {"snapshot_id": "s", "points": []}, {}, _RESULTS_REFUSED
    )["outputs"]
    assert refused["total"] == 0
    assert refused["refusal"] == {"code": "no_program", "message": "нет программы"}
    # покрытие станции вердикт не трогает
    assert build_report(_entry(), {"snapshot_id": "s", "points": []}, {}, results)["verdict"] == "partial"


# -- HTTP-слой ---------------------------------------------------------------


def _station_routes(snapshot_body, calculation_body):
    async def _nothing():
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/telemetry/snapshots":
            return httpx.Response(201, json=snapshot_body)
        if request.method == "POST" and path.endswith("/calculate"):
            return httpx.Response(200, json={"snapshot_id": snapshot_body["snapshot_id"], "cached": False})
        if request.method == "GET" and path.endswith("/results"):
            return httpx.Response(200, json=calculation_body)
        if request.method == "GET" and path.startswith("/api/telemetry/snapshots/"):
            return httpx.Response(200, json=snapshot_body)
        return httpx.Response(404, json={"detail": "no route"})

    return httpx.MockTransport(handler)


def _full_snapshot(snapshot_id: str = "snap-1"):
    points = [
        _point(signal_id, value=value)
        for signal_id, value in BASELINE.items()
    ]
    return {
        "snapshot_id": snapshot_id,
        "project_id": "124_NSH2_PS",
        "project_revision": "rev-1",
        "capture_mode": "live_batch",
        "captured_at": "2026-03-23T15:05:00+00:00",
        "capture_window_ms": 0,
        "value_spread_ms": 0,
        "consistency": "best_effort_consistent",
        "telemetry_config_version": "v1",
        "connection_ids": ["plc-a"],
        "tags_total": len(points),
        "tags_received": len(points),
        "tags_missing": 0,
        "tags_bad_quality": 0,
        "tags_untyped": 0,
        "points": points,
    }


def _stale_snapshot(snapshot_id: str):
    """Снимок, где станция не видела точки за час до снимка — гейт должен
    отправить такой на переснятие."""
    body = _full_snapshot(snapshot_id)
    for point in body["points"]:
        point["received_at"] = "2026-03-23T14:05:00+00:00"
    return body


# Форма ответа GET /api/telemetry/snapshots/{id}/results — именно её
# разбирает outputs_observation (outputs_total/computed + composition).
_RESULTS = {
    "snapshot_id": "snap-1",
    "project_revision": "rev-1",
    "program_snapshot_id": "psn-1",
    "mode": "live_batch",
    "source": "snapshot",
    "created_at": "2026-03-23T15:05:01+00:00",
    "refusal": None,
    "bindings_total": 1,
    "bindings_resolved": 1,
    "outputs_total": 1,
    "outputs_computed": 1,
    "composition": {
        "by_status": {"calculated": 1},
        "missing_initial_state": {},
        "unsupported_reasons": {},
    },
    "rows": [
        {
            "tag_id": "out-1",
            "symbol_id": None,
            "role": "output",
            "actual_value": True,
            "expected_value": True,
            "status": "calculated",
            "reason_code": None,
            "effective_quality": "good",
            "expression": "x",
            "pou": "P",
            "dependencies": [],
            "explanation": "",
            "calculated_at": "2026-03-23T15:05:01+00:00",
            "actual_timestamp": None,
        }
    ],
}

# Расчёт с отказом: состав пуст, отказ проходит в наблюдение как есть.
_RESULTS_REFUSED = {
    "snapshot_id": "snap-1",
    "project_revision": "rev-1",
    "refusal": {"code": "no_program", "message": "нет программы"},
    "outputs_total": None,
    "outputs_computed": None,
    "composition": {"by_status": {}},
    "rows": [],
}


@pytest.fixture()
def verify_env(tmp_path, monkeypatch):
    import app.api.verification_routes as vr
    import app.main as main_module
    from app.api.station_routes import set_station_proxy
    from app.services.station_proxy import StationProxy

    monkeypatch.setattr(settings, "VERIFICATION_DIR", str(tmp_path / "verifications"))
    monkeypatch.setattr(vr, "RECAPTURE_PAUSE_S", 0.0)  # тесты не ждут реальных секунд
    main_module._ledger.clear()
    original = main_module._station_proxy
    proxy = StationProxy("http://station.test", transport=_station_routes(_full_snapshot(), _RESULTS))
    set_station_proxy(proxy)
    yield main_module
    set_station_proxy(original)
    main_module._ledger.clear()


def _client():
    from app.main import app

    return TestClient(app)


def test_report_captures_snapshot_calculates_and_passes(verify_env):
    client = _client()
    entry = _entry(causes=[{
        "signal_id": "pu-s2", "name": "Стоп", "project_tag": "tag-pu-s2",
        "before": True, "after": False,
    }])
    # pu-s2 в базисе False: снапшот собран из базиса, причина видима и равна
    resp = client.post("/api/verification/report", json={"entry": entry})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "pass"
    assert body["cause_check"]["counts"]["match"] == 1
    assert body["state_check"]["missing"] == 0
    assert body["outputs"]["total"] == 1
    assert body["outputs"]["computed"] == 1
    assert body["outputs"]["by_status"] == {"calculated": 1}

    saved = list(Path(settings.VERIFICATION_DIR).glob("*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8"))["verdict"] == "pass"

    listed = client.get("/api/verification/list").json()
    assert listed["total"] == 1
    assert listed["items"][0]["verdict"] == "pass"
    assert listed["items"][0]["kind"] == "estop_console"


def test_report_against_existing_snapshot_and_bad_entry(verify_env):
    client = _client()
    resp = client.post(
        "/api/verification/report",
        json={"entry": _entry(), "snapshot_id": "snap-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["snapshot"]["id"] == "snap-1"

    missing_ts = client.post(
        "/api/verification/report", json={"entry": {"run_id": "x"}}
    )
    assert missing_ts.status_code == 400


def test_report_returns_502_when_station_down(verify_env):
    from app.api.station_routes import set_station_proxy
    from app.services.station_proxy import StationProxy

    def broken(request):
        raise httpx.ConnectError("refused", request=request)

    set_station_proxy(StationProxy("http://station.test", transport=httpx.MockTransport(broken)))
    resp = _client().post("/api/verification/report", json={"entry": _entry()})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "station_unreachable"


# -- гейт свежести снимка -----------------------------------------------------


def _snapshot_queue_transport(bodies):
    """POST /snapshots раздаёт тела по очереди (последнее повторяется)."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/telemetry/snapshots":
            index = min(state["calls"], len(bodies) - 1)
            state["calls"] += 1
            return httpx.Response(201, json=bodies[index])
        if request.method == "POST" and path.endswith("/calculate"):
            return httpx.Response(200, json={"cached": False})
        if request.method == "GET" and path.endswith("/results"):
            return httpx.Response(200, json=_RESULTS)
        return httpx.Response(404, json={"detail": "no route"})

    return httpx.MockTransport(handler), state


def test_report_recaptures_stale_snapshot_once(verify_env, monkeypatch):
    from app.api.station_routes import set_station_proxy
    from app.services.station_proxy import StationProxy

    transport, state = _snapshot_queue_transport(
        [_stale_snapshot("snap-stale"), _full_snapshot("snap-1")]
    )
    set_station_proxy(StationProxy("http://station.test", transport=transport))

    resp = _client().post("/api/verification/report", json={"entry": _entry()})
    assert resp.status_code == 200
    body = resp.json()
    # Протухший снимок переснят один раз, в отчёте — свежий.
    assert state["calls"] == 2
    assert body["snapshot_recaptures"] == 1
    assert body["snapshot"]["id"] == "snap-1"
    assert "stale_points" not in body


def test_report_records_stale_points_when_recapture_exhausted(verify_env):
    from app.api.station_routes import set_station_proxy
    from app.services.station_proxy import StationProxy

    transport, state = _snapshot_queue_transport(
        [_stale_snapshot("snap-a"), _stale_snapshot("snap-b")]
    )
    set_station_proxy(StationProxy("http://station.test", transport=transport))

    body = _client().post("/api/verification/report", json={"entry": _entry()}).json()
    # Два переснятия не спасли — факт честно остаётся в отчёте.
    assert state["calls"] == 3  # начальный + MAX_RECAPTURES
    assert body["snapshot_recaptures"] == 2
    assert body["snapshot"]["id"] == "snap-b"
    assert len(body["stale_points"]) == len(BASELINE)


def test_report_with_explicit_snapshot_id_skips_the_gate(verify_env):
    from app.api.station_routes import set_station_proxy
    from app.services.station_proxy import StationProxy

    transport, state = _snapshot_queue_transport([_stale_snapshot("snap-stale")])
    # GET указанного снимка отвечает тем же телом
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/snapshots/snap-stale"):
            return httpx.Response(200, json=_stale_snapshot("snap-stale"))
        return transport.handler(request)

    set_station_proxy(StationProxy("http://station.test", transport=httpx.MockTransport(handler)))
    resp = _client().post(
        "/api/verification/report", json={"entry": _entry(), "snapshot_id": "snap-stale"}
    )
    assert resp.status_code == 200
    # Явный снимок не переснялся: ни одного POST /snapshots.
    assert state["calls"] == 0
    assert "snapshot_recaptures" not in resp.json()
