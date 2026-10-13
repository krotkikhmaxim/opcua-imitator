"""Прокси станции: read-only проход и его содержательные решения.

Проверяется на httpx.MockTransport: запрос уходит с тем же методом, путём,
query и телом; ответ приходит с исходным статусом и типом содержимого;
недоступность станции — 502 station_unreachable, а не притворный пустой
ответ; снимок без проекта получает проект станции по умолчанию. Сквозной
HTTP-тест подменяет прокси приложения на мок и возвращает прежний.
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

from app.services.station_proxy import StationProxy, StationUnreachable

BINDINGS_BODY = {"bindings": [{"id": "ch1-10a3-k1", "state": "active", "bound": True}]}


class RecordingTransport(httpx.MockTransport):
    """Мок, запоминающий каждый запрос и отвечающий по таблице маршрутов."""

    def __init__(self, routes):
        self.requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            route = routes.get(request.url.path)
            if route is None:
                return httpx.Response(404, json={"detail": "no route"})
            status, payload = route
            return httpx.Response(status, json=payload)

        super().__init__(handler)

    @property
    def last(self):
        return self.requests[-1]


# -- клиент ------------------------------------------------------------------


def test_proxy_forwards_method_path_and_query():
    tp = RecordingTransport({"/api/telemetry/bindings": (200, BINDINGS_BODY)})
    proxy = StationProxy("http://station.test", transport=tp)

    async def scenario():
        try:
            resp = await proxy.request("GET", "/api/telemetry/bindings", params={"limit": "5"})
            assert resp.status_code == 200
            assert resp.json() == BINDINGS_BODY
        finally:
            await proxy.aclose()

    asyncio.run(scenario())
    req = tp.last
    assert req.method == "GET"
    assert str(req.url) == "http://station.test/api/telemetry/bindings?limit=5"


def test_proxy_reports_transport_failure_as_unreachable():
    def broken(request):
        raise httpx.ConnectError("connection refused", request=request)

    proxy = StationProxy("http://station.test", transport=httpx.MockTransport(broken))

    async def scenario():
        try:
            with pytest.raises(StationUnreachable):
                await proxy.request("GET", "/health")
        finally:
            await proxy.aclose()

    asyncio.run(scenario())


# -- HTTP-слой ---------------------------------------------------------------


@pytest.fixture()
def swap_station_proxy():
    """Подменяет прокси приложения на мок, после теста возвращает прежний."""
    import app.main as main_module
    from app.api.station_routes import set_station_proxy

    original = main_module._station_proxy

    def install(proxy):
        set_station_proxy(proxy)

    yield install
    set_station_proxy(original)


def _client():
    from app.main import app

    return TestClient(app)


def test_routes_forward_query_body_and_upstream_status(swap_station_proxy):
    tp = RecordingTransport({
        "/api/telemetry/bindings": (200, BINDINGS_BODY),
        "/api/telemetry/snapshots": (201, {"id": "snap-1"}),
    })
    swap_station_proxy(StationProxy("http://station.test", transport=tp))
    client = _client()

    listed = client.get("/api/station/bindings", params={"limit": "5"})
    assert listed.status_code == 200
    assert listed.json()["bindings"][0]["id"] == "ch1-10a3-k1"
    assert listed.headers["content-type"].startswith("application/json")
    assert tp.requests[-1].url.path == "/api/telemetry/bindings"
    assert tp.requests[-1].url.params["limit"] == "5"

    # без проекта и режима — подставляются значения по умолчанию
    created = client.post("/api/station/snapshots", json={})
    assert created.status_code == 201
    sent = json.loads(tp.last.content)
    from app.config import settings

    assert sent["project_id"] == settings.STATION_PROJECT_ID
    assert sent["mode"] == "live_batch"

    # явные значения оператора не переписываются
    client.post("/api/station/snapshots", json={"project_id": "other", "mode": "history"})
    sent2 = json.loads(tp.last.content)
    assert sent2["project_id"] == "other"
    assert sent2["mode"] == "history"


def test_station_down_returns_502_station_unreachable(swap_station_proxy):
    def broken(request):
        raise httpx.ConnectError("connection refused", request=request)

    swap_station_proxy(StationProxy("http://station.test", transport=httpx.MockTransport(broken)))
    client = _client()

    resp = client.get("/api/station/health")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "station_unreachable"


def test_write_side_stays_closed_by_whitelist(swap_station_proxy):
    tp = RecordingTransport({})
    swap_station_proxy(StationProxy("http://station.test", transport=tp))
    client = _client()

    # у ручки значений нет POST — маршрут не пересылается, метится своим роутером
    assert client.post("/api/station/values").status_code in (404, 405)
    assert client.post("/api/station/bindings").status_code in (404, 405)
    assert tp.requests == []  # до станции не дошло ничего


def test_raster_content_type_passes_through(swap_station_proxy):
    def handler(request):
        return httpx.Response(200, content=b"\x89PNG-fake", headers={"content-type": "image/png"})

    swap_station_proxy(StationProxy("http://station.test", transport=httpx.MockTransport(handler)))
    client = _client()

    resp = client.get("/api/station/schematic/pages/1/raster")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNG-fake"
