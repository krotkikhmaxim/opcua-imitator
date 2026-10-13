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


def _point(signal_id, *, value=None, presence="present", tag=None):
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
        "received_at": None,
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

    calculation = {
        "counts": {"output": 2, "input": 3},
        "cause_breakdown": {"no_writer": 1},
        "coverage": 0.5,
        "results": [
            {"role": "input", "status": "input_from_telemetry"},
            {"role": "output", "status": "calculated", "reason_code": None},
            {"role": "output", "status": "missing_initial_state", "reason_code": "no_writer"},
        ],
    }
    outputs = build_report(_entry(), {"snapshot_id": "s", "points": []}, {}, calculation)[
        "outputs"
    ]
    assert outputs["total"] == 2
    assert outputs["computed"] == 1
    assert outputs["by_status"] == {"calculated": 1, "missing_initial_state": 1}
    assert outputs["reason_breakdown"] == {"no_writer": 1}
    assert outputs["coverage"] == 0.5
    # покрытие станции вердикт не трогает
    assert build_report(_entry(), {"snapshot_id": "s", "points": []}, {}, calculation)["verdict"] == "partial"


# -- HTTP-слой ---------------------------------------------------------------


def _station_routes(snapshot_body, calculation_body):
    async def _nothing():
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/telemetry/snapshots":
            return httpx.Response(201, json=snapshot_body)
        if request.method == "POST" and path.endswith("/calculate"):
            return httpx.Response(200, json=calculation_body)
        if request.method == "GET" and path.startswith("/api/telemetry/snapshots/"):
            return httpx.Response(200, json=snapshot_body)
        return httpx.Response(404, json={"detail": "no route"})

    return httpx.MockTransport(handler)


def _full_snapshot():
    points = [
        _point(signal_id, value=value)
        for signal_id, value in BASELINE.items()
    ]
    return {
        "snapshot_id": "snap-1",
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


_CALC = {
    "snapshot_id": "snap-1",
    "project_revision": "rev-1",
    "cached": False,
    "calculated_at": "2026-03-23T15:05:01+00:00",
    "counts": {"output": 1},
    "evaluated_count": 1,
    "not_evaluated_count": 0,
    "cause_breakdown": {},
    "coverage": 1.0,
    "capture_window_ms": 0,
    "value_spread_ms": 0,
    "consistency": "best_effort_consistent",
    "results": [
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


@pytest.fixture()
def verify_env(tmp_path, monkeypatch):
    import app.main as main_module
    from app.api.station_routes import set_station_proxy
    from app.services.station_proxy import StationProxy

    monkeypatch.setattr(settings, "VERIFICATION_DIR", str(tmp_path / "verifications"))
    main_module._ledger.clear()
    original = main_module._station_proxy
    proxy = StationProxy("http://station.test", transport=_station_routes(_full_snapshot(), _CALC))
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
    assert body["outputs"]["computed"] == 1

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
