"""Сверка с журналом правды: снапшот станции → расчёт → отчёт.

Маршрут тонкий: он умеет только снять снапшот (или взять указанный),
попросить станцию рассчитать его, спросить у журнала записей ожидаемое
состояние на момент снимка и собрать отчёт средствами
``verification.build_report``. Станция недоступна — 502, её собственная
ошибка проходит с её же статусом; отказ расчёта отчёт не сносит — секция
``outputs`` честно пустеет, cause/state сверяются дальше.

Ожидаемое состояние берётся на момент **снимка** (``captured_at``), а не
остановки: в окне остановки стенд легально продолжает писать (выбег
энкодеров, торможение), и требовать от станции значения, которое появится
на шине позже снимка, было бы требованием знать будущее. Причина по-прежнему
сверяется на ``stopped_at`` — у неё своя семантика видимости.

Коллектор станции доставляет значения порциями (WAL-сброс каждые 5 с),
поэтому свежесть снимка проверяется гейтом: точки, которых станция не
видела дольше ``STALE_WINDOW_S`` до снимка, — повод переснять один раз;
переснятый снимок и перечень протухших точек честно попадают в отчёт.
"""

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

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

#: Точка считается протухшей, если станция не видела её дольше этого окна
#: до момента снимка. Структурная задержка доставки на станции — ~10 с
#: (fsync на каждый сэмпл + сброс WAL раз в 5 с), окно должно её пропускать
#: и ловить именно выпадение сигнала из доставки (десятки секунд и больше).
STALE_WINDOW_S = 15.0
#: Пауза перед переснятием: ждём ближайший сброс WAL (5 с) с запасом.
RECAPTURE_PAUSE_S = 4.0
#: Сколько раз можно переснять снимок внутри окна остановки (20 с).
MAX_RECAPTURES = 2

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


async def _station_json(
    method: str, path: str, json_body: Any = None, timeout_s: float = 10.0
) -> Any:
    try:
        resp = await get_station_proxy().request(method, path, json=json_body, timeout_s=timeout_s)
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


def _stale_points(snapshot: Dict[str, Any], window_s: float = STALE_WINDOW_S) -> List[str]:
    """Binding-и точек, которых станция не видела дольше окна до снимка.

    Свежесть измеряется по ``received_at`` (когда станция наблюдала точку),
    а не по ``source_timestamp`` (когда значение менялось на шине): статичный
    сигнал с давней меткой источника — норма, давняя доставка — нет.
    """
    captured = snapshot.get("captured_at")
    if not isinstance(captured, str):
        return []
    try:
        captured_dt = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError:
        return []
    stale: List[str] = []
    for point in snapshot.get("points") or []:
        received = point.get("received_at")
        if not isinstance(received, str):
            stale.append(str(point.get("binding_id")))
            continue
        try:
            age = (captured_dt - datetime.fromisoformat(received)).total_seconds()
        except ValueError:
            stale.append(str(point.get("binding_id")))
            continue
        if age > window_s:
            stale.append(str(point.get("binding_id")))
    return stale


def _safe_name(report: Dict[str, Any]) -> str:
    stamp = datetime.now().strftime(settings.TIME_FORMAT)
    entry = report["entry"]
    run = re.sub(r"[^A-Za-z0-9_-]+", "_", str(entry.get("run_id") or "run"))[:40]
    return f"{stamp}_{run}_ep{entry.get('episode')}.json"


@router.post("/report")
async def report(req: VerifyRequest) -> Dict[str, Any]:
    ledger = _services()
    marker = _parse_marker(req.entry.get("stopped_at"))

    recaptures = 0
    if req.snapshot_id:
        snapshot = await _station_json("GET", f"/api/telemetry/snapshots/{req.snapshot_id}")
    else:
        snapshot = await _station_json(
            "POST",
            "/api/telemetry/snapshots",
            {"mode": "live_batch", "project_id": settings.STATION_PROJECT_ID},
        )
        # Гейт свежести: коллектор станции доставляет значения порциями, и
        # снимок мог застать registry до очередной порции. Переснимаем, пока
        # окно остановки позволяет; операторский snapshot_id не трогаем —
        # это явный выбор «сверить именно этот».
        while recaptures < MAX_RECAPTURES and _stale_points(snapshot):
            await asyncio.sleep(RECAPTURE_PAUSE_S)
            snapshot = await _station_json(
                "POST",
                "/api/telemetry/snapshots",
                {"mode": "live_batch", "project_id": settings.STATION_PROJECT_ID},
            )
            recaptures += 1
    if recaptures:
        report_meta = {"snapshot_recaptures": recaptures}
    else:
        report_meta = {}
    stale = _stale_points(snapshot)
    if stale:
        report_meta["stale_points"] = stale
    snapshot_id = snapshot.get("snapshot_id")

    # Ожидаемое состояние — на момент снимка: стенд мог легально писать в
    # окне (выбег), и снимок отражает шину на свой момент. Не читается
    # captured_at (или нечитаем) — падаем на stopped_at как раньше.
    captured_at = snapshot.get("captured_at")
    if isinstance(captured_at, str):
        try:
            marker = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            pass

    calculation: Optional[Dict[str, Any]] = None
    outputs_error: Optional[Dict[str, Any]] = None
    if snapshot_id:
        try:
            # calculate запускает расчёт, результаты читаются отдельной ручкой:
            # именно там живут outputs_total/computed и composition.
            # Расчёт тяжёлый — свой бюджет времени, не 10-секундный умолчание.
            await _station_json(
                "POST", f"/api/telemetry/snapshots/{snapshot_id}/calculate", timeout_s=60.0
            )
            calculation = await _station_json(
                "GET", f"/api/telemetry/snapshots/{snapshot_id}/results", timeout_s=60.0
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
    report.update(report_meta)
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
