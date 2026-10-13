"""Сверка с журналом правды: снапшот станции → расчёт → отчёт.

Маршрут тонкий: он умеет только снять снапшот (или взять указанный),
попросить станцию рассчитать его, спросить у журнала записей ожидаемое
состояние на момент остановки и собрать отчёт средствами
``verification.build_report``. Станция недоступна — 502, её собственная
ошибка проходит с её же статусом; отказ расчёта отчёт не сносит — секция
``outputs`` честно пустеет, cause/state сверяются дальше.
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.station_routes import get_station_proxy
from app.config import settings
from app.services.scenario.hoist import BASELINE
from app.services.station_proxy import StationUnreachable
from app.services.verification import build_report
from app.services.drivers.ledger import WriteLedger
from app.services.ws import manager

router = APIRouter(prefix="/api/verification", tags=["verification"])

_ledger: Optional[WriteLedger] = None


def set_verification_services(ledger: WriteLedger) -> None:
    global _ledger
    _ledger = ledger


def _services() -> WriteLedger:
    if _ledger is None:
        raise RuntimeError("Службы сверки не инициализированы")
    return _ledger


class VerifyRequest(BaseModel):
    entry: Dict[str, Any]
    #: Проверить уже снятый снапшот вместо захвата нового (повторная сверка).
    snapshot_id: Optional[str] = None


def _parse_marker(stopped_at: Any) -> datetime:
    if not isinstance(stopped_at, str):
        raise HTTPException(status_code=400, detail="В записи журнала нет stopped_at")
    try:
        return datetime.fromisoformat(stopped_at)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Нечитаемая метка stopped_at: {stopped_at}")


async def _station_json(method: str, path: str, json_body: Any = None) -> Any:
    try:
        resp = await get_station_proxy().request(method, path, json=json_body)
    except StationUnreachable as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "station_unreachable", "message": f"Станция недоступна: {exc}"},
        )
    if resp.status_code >= 400:
        # Ошибка станции проходит как есть: она авторитетна в своём ответе.
        try:
            detail = resp.json()
        except Exception:
            detail = {"code": "station_error", "message": resp.text[:300]}
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


def _safe_name(report: Dict[str, Any]) -> str:
    stamp = datetime.now().strftime(settings.TIME_FORMAT)
    entry = report["entry"]
    run = re.sub(r"[^A-Za-z0-9_-]+", "_", str(entry.get("run_id") or "run"))[:40]
    return f"{stamp}_{run}_ep{entry.get('episode')}.json"


@router.post("/report")
async def report(req: VerifyRequest) -> Dict[str, Any]:
    ledger = _services()
    marker = _parse_marker(req.entry.get("stopped_at"))

    if req.snapshot_id:
        snapshot = await _station_json("GET", f"/api/telemetry/snapshots/{req.snapshot_id}")
    else:
        snapshot = await _station_json(
            "POST",
            "/api/telemetry/snapshots",
            {"mode": "live_batch", "project_id": settings.STATION_PROJECT_ID},
        )
    snapshot_id = snapshot.get("snapshot_id")

    calculation: Optional[Dict[str, Any]] = None
    outputs_error: Optional[Dict[str, Any]] = None
    if snapshot_id:
        try:
            # calculate запускает расчёт, результаты читаются отдельной ручкой:
            # именно там живут outputs_total/computed и composition.
            await _station_json("POST", f"/api/telemetry/snapshots/{snapshot_id}/calculate")
            calculation = await _station_json(
                "GET", f"/api/telemetry/snapshots/{snapshot_id}/results"
            )
        except HTTPException as exc:
            outputs_error = {
                "status": exc.status_code,
                "detail": exc.detail,
            }

    report = build_report(
        req.entry,
        snapshot,
        ledger.expected_state_at(marker, BASELINE),
        calculation=calculation,
    )
    if outputs_error is not None:
        report["outputs_error"] = outputs_error
    report["generated_at"] = datetime.now().astimezone().isoformat()

    target = _verification_dir() / _safe_name(report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    await manager.broadcast({
        "type": "verification_report",
        "file": target.name,
        "verdict": report["verdict"],
        "kind": report["entry"].get("kind"),
    })
    return report


def _verification_dir():
    from pathlib import Path

    return Path(settings.VERIFICATION_DIR)


@router.get("/list")
def list_reports(limit: int = 50) -> Dict[str, Any]:
    """Сохранённые отчёты, новые первыми — только сводки, не тела отчётов."""
    directory = _verification_dir()
    items: list[Dict[str, Any]] = []
    if directory.exists():
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[: max(0, limit)]:
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append({
                "file": path.name,
                "generated_at": body.get("generated_at"),
                "verdict": body.get("verdict"),
                "kind": (body.get("entry") or {}).get("kind"),
                "title": (body.get("entry") or {}).get("title"),
                "run_id": (body.get("entry") or {}).get("run_id"),
                "episode": (body.get("entry") or {}).get("episode"),
                "cause_counts": (body.get("cause_check") or {}).get("counts"),
                "state_mismatched": ((body.get("state_check") or {}).get("value_mismatched")),
            })
    return {"items": items, "total": len(items)}
