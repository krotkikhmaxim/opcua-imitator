"""HTTP-прокси станции: белый список read-only ручек под /api/station.

Соответствие путей — один к одному с ручками станции после срезания
префикса. Тело ответа уходит с исходным статусом и content-type
(растры схем — картинка, расчёты — JSON); страница ошибки не
переписывается. Все маршруты только читают станцию и делают ровно один
исходящий запрос.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.station_proxy import StationProxy, StationUnreachable

router = APIRouter(prefix="/api/station", tags=["station"])

_proxy: Optional[StationProxy] = None


def set_station_proxy(proxy: Optional[StationProxy]) -> None:
    global _proxy
    _proxy = proxy


def _proxy_required() -> StationProxy:
    if _proxy is None:
        raise RuntimeError("Прокси станции не инициализирован")
    return _proxy


async def _forward(
    method: str,
    path: str,
    *,
    request: Request,
    json: Any = None,
) -> Response:
    try:
        upstream = await _proxy_required().request(
            method, path, params=dict(request.query_params), json=json
        )
    except StationUnreachable as exc:
        return JSONResponse(
            status_code=502,
            content={
                "detail": {
                    "code": "station_unreachable",
                    "message": f"Станция недоступна: {exc}",
                }
            },
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@router.get("/health")
async def health(request: Request) -> Response:
    return await _forward("GET", "/health", request=request)


@router.get("/values")
async def values(request: Request) -> Response:
    return await _forward("GET", "/api/telemetry/values", request=request)


@router.get("/bindings")
async def bindings(request: Request) -> Response:
    return await _forward("GET", "/api/telemetry/bindings", request=request)


@router.get("/snapshots")
async def snapshots_list(request: Request) -> Response:
    return await _forward("GET", "/api/telemetry/snapshots", request=request)


@router.get("/snapshots/{snapshot_id}")
async def snapshot_get(snapshot_id: str, request: Request) -> Response:
    return await _forward("GET", f"/api/telemetry/snapshots/{snapshot_id}", request=request)


@router.post("/snapshots")
async def snapshot_create(request: Request) -> Response:
    """Снимок состояния; без явного проекта подставляется проект станции."""
    body = await request.json() if await request.body() else {}
    if not isinstance(body, dict):
        body = {}
    body.setdefault("mode", "live_batch")
    if not body.get("project_id"):
        body["project_id"] = settings.STATION_PROJECT_ID
    return await _forward("POST", "/api/telemetry/snapshots", request=request, json=body)


@router.post("/snapshots/{snapshot_id}/calculate")
async def snapshot_calculate(snapshot_id: str, request: Request) -> Response:
    return await _forward(
        "POST", f"/api/telemetry/snapshots/{snapshot_id}/calculate", request=request
    )


@router.get("/calculations")
async def calculations(request: Request) -> Response:
    return await _forward("GET", "/api/telemetry/calculations", request=request)


@router.get("/schematic/status")
async def schematic_status(request: Request) -> Response:
    return await _forward("GET", "/api/schematic/status", request=request)


@router.get("/schematic/pages")
async def schematic_pages(request: Request) -> Response:
    return await _forward("GET", "/api/schematic/pages", request=request)


@router.get("/schematic/pages/{number}")
async def schematic_page(number: str, request: Request) -> Response:
    return await _forward("GET", f"/api/schematic/pages/{number}", request=request)


@router.get("/schematic/pages/{number}/raster")
async def schematic_raster(number: str, request: Request) -> Response:
    return await _forward("GET", f"/api/schematic/pages/{number}/raster", request=request)


@router.get("/schematic/nets/{label}")
async def schematic_nets(label: str, request: Request) -> Response:
    return await _forward("GET", f"/api/schematic/nets/{label}", request=request)


@router.get("/schematic/locations")
async def schematic_locations(request: Request) -> Response:
    return await _forward("GET", "/api/schematic/locations", request=request)
