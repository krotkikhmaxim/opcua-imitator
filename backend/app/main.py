import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api.diagnostics_routes import router as diagnostics_router, set_diagnostics_journal
from app.api.playback_routes import router as playback_router, set_playback_service
from app.api.routes import router, set_services, set_snapshot_hook
from app.api.scenario_routes import (
    ensure_manual_writes_allowed,
    get_scenario_engine,
    router as scenario_router,
    set_gate,
    set_playback_stop,
    set_registry,
    set_scenario_engine,
)
from app.api.station_routes import router as station_router, set_station_proxy
from app.api.schematic_routes import router as schematic_router
from app.api.verification_routes import (
    router as verification_router,
    set_verification_services,
)
from app.config import load_signal_config, settings
from app.services import signal_types
from app.services.diagnostics import DiagnosticsJournal
from app.services.drivers import HeartbeatGate, ValueDriverRegistry, WriteLedger
from app.services.heartbeat import HeartbeatService
from app.services.opc_bus import OPCMode, OPCSignalBus
from app.services.ownership import OwnershipRegistry
from app.services.persistence import SnapshotStore, registry_fingerprint
from app.services.playback.service import PlaybackService
from app.services.scenario import ScenarioEngine
from app.services.scenario.hoist import BASELINE
from app.services.signal_cache import SignalCache
from app.services.station_proxy import StationProxy
from app.services.storage import StateStorage
from app.services.values_feed import ValuesFeed
from app.services.ws import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("opcua-imitator")

app = FastAPI(title="OPC UA State Imitator", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_configs = load_signal_config(settings.SIGNAL_CONFIG_PATH)
_cache = SignalCache(_configs)
_bus = OPCSignalBus(_cache, mode=os.getenv("OPC_UA_MODE", OPCMode.SERVER))
_storage = StateStorage(settings.SAVED_STATES_DIR)
# Heartbeat (cyclic refresh) создаётся в startup для режима SERVER; вне его —
# None, и API сообщает о недоступности режима.
_heartbeat: Optional[HeartbeatService] = None
# Драйверы значений: ровно один активный источник записей (ручной/сценарий/
# плейбэк), журнал всех timestamped-записей для сверки со станцией и шлюз,
# глушащий heartbeat на время драйвера (иначе он перезаписывает
# SourceTimestamp и портит шкалу события).
_registry = ValueDriverRegistry()
_ledger = WriteLedger()
_gate = HeartbeatGate(_registry, lambda: _heartbeat)
# Владение сигналами (ТЗ #5 §9.2): сценарий объявляет набор до старта и
# держит его исключительно; шина проверяет владение при каждой записи.
_ownership = OwnershipRegistry()
_bus.set_ownership(_ownership)
# Диагностика (ТЗ #5, шаг 9 — §14): операции применения и жизненный цикл
# сценариев в одном ограниченном журнале; наружу — GET /api/diagnostics/events.
_diagnostics = DiagnosticsJournal()
set_diagnostics_journal(_diagnostics)
# Снимок подтверждённого состояния (ТЗ #5, шаг 8 — §13): атомарный файл,
# команды/жизненный цикл сценария/останов сервера сохраняют, старт —
# восстанавливает (повреждённый или чужой снимок = начальные значения
# с диагностикой; сценарии после рестарта не возобновляются).
_snapshot_store = SnapshotStore(
    path=Path(settings.PERSIST_SNAPSHOT_PATH),
    fingerprint=registry_fingerprint(_cache.config),
    signal_ids=[cfg["id"] for cfg in _cache.config],
    read_card=lambda sid: (
        _cache.get_state(sid).card()
        if _cache.get_state(sid) is not None
        else {"value": None, "revision": 0, "source": None, "changed_at": None}
    ),
    interval_s=settings.PERSIST_INTERVAL_S,
    active=lambda: (engine.running if (engine := get_scenario_engine()) else False),
)

# Журнал записей теперь доходит и до загрузки состояний: routes.load_state
# отмечает восстановленные значения в сверке со станцией (замечено аудитом
# §14 — проводка была, вызов без ledger).
set_services(_bus, _storage, ledger=_ledger, diagnostics=_diagnostics)
# Загрузка сохранённого состояния (routes.load_state) — тоже граница
# применения; после неё снимок переносит загруженные значения (§13.2).
set_snapshot_hook(_snapshot_store.save)
app.include_router(router)
app.include_router(diagnostics_router)
# Сценарии пишут через ту же шину; в режиме real движок недоступен — запись
# ушла бы во внешний сервер, то есть в настоящий ПЛК. Писатель обёрнут
# журналом: движок не знает о нём, а сверка со станцией получает все записи.
# Каждый прогон пишет со своим source/owner «scenario:<run_id>» (§9.3):
# карточки показывают, чьё это изменение, а владение пропускает записи
# только самого владельца.
def _scenario_writer_factory(run_id: str) -> Any:
    owner_id = f"scenario:{run_id}"

    def writer(signal_id: str, value: Any, when: datetime) -> Any:
        return _bus.write_value_at(signal_id, value, when, source=owner_id, owner=owner_id)

    return _ledger.wrap(writer)


set_scenario_engine(
    ScenarioEngine(
        _scenario_writer_factory,
        _cache.config,
        simulated=_bus.simulated,
        ownership=_ownership,
        journal_path=Path(settings.SCENARIO_JOURNAL_PATH),
        on_transition=lambda _phase: _snapshot_store.save(),
        on_event=_diagnostics.record_scenario,
    )
)
app.include_router(scenario_router)
set_registry(_registry)
set_gate(_gate)
_registry.register(
    "scenario", lambda: (engine.running if (engine := get_scenario_engine()) else False)
)
# Плейбэк трендов пишет через тот же обёрнутый журналом writer, что и
# сценарий: одна дверь записи, один журнал для сверки. В реестре он держит
# шину от запуска до остановки/финала — пауза шину не отпускает.
# Заморозка таймлайна (ТЗ #5, §2): по умолчанию запись из тренда в рабочее
# состояние сигналов запрещена — PLAYBACK_WRITES_ENABLED=true её снимает.
_playback = PlaybackService(
    _ledger.wrap(_bus.write_value_at),
    writes_enabled=settings.PLAYBACK_WRITES_ENABLED,
)
_registry.register("playback", lambda: _playback.bus_held)
set_playback_service(
    _playback, lambda: {cfg["id"]: cfg for cfg in _cache.config},
    upload_dir=Path(settings.PLAYBACK_UPLOAD_DIR),
)
# Применение сценария/режима останавливает идущий таймлайн сам, а не
# отбивает запуск конфликтом 409: шину освобождает тот же service.stop().
set_playback_stop(lambda: _playback.stop() if _playback.bus_held else None)
app.include_router(playback_router)
# Станция — наблюдатель: прокси только читает её API (белый список под
# /api/station), CORS у станции нет по умолчанию и не появляется.
_station_proxy = StationProxy(settings.STATION_URL, timeout_s=settings.STATION_TIMEOUT_S)
set_station_proxy(_station_proxy)
app.include_router(station_router)
# Мост «обозначение Э3 → сигналы» — read-only раздача готового артефакта.
app.include_router(schematic_router)
# Сверка с журналом правды: тот же журнал записей (ожидаемое состояние на
# момент остановки); прокси сверка берёт актуальный у station_routes.
set_verification_services(_ledger)
app.include_router(verification_router)
# WS-фид дельт: тот же реестр (смена активного драйвера уходит в WS) и тот же
# шлюз (тик пересчитывает heartbeat, если драйвер остановился мимо маршрута).
# §12: дельты несут ревизию и источник каждой смены, каждый тик — с seq;
# карта владельцев уходит при смене версии реестра владения.
_values_feed = ValuesFeed(
    _bus.read_value,
    [cfg["id"] for cfg in _cache.config],
    manager.broadcast,
    settings.VALUES_FEED_INTERVAL_S,
    registry=_registry,
    gate=_gate,
    revision_of=_cache.get_revision,
    source_of=lambda sid: (
        state.source if (state := _cache.get_state(sid)) is not None else None
    ),
    ownership=_ownership,
)


async def _restore_snapshot() -> None:
    """Восстановить последний снимок при старте (§13.3).

    Снимок проверяется целиком: повреждённый или несовместимый отбрасывается,
    сервер стартует с начальными значениями и явной диагностикой. Сценарии
    после рестарта не возобновляются, владение пусто — источник последнего
    значения в карточках не путать с владельцем управления.
    """
    document, reason = _snapshot_store.load()
    if document is None:
        logger.warning("старт с начальными значениями: %s", reason)
        return
    restored = _cache.restore(document["signals"])
    await _bus.restore_values(
        {sid: card["v"] for sid, card in document["signals"].items()}
    )
    logger.info(
        "восстановлен снимок от %s (%d сигналов)", document.get("created_at"), restored
    )


@app.on_event("startup")
async def startup() -> None:
    await _bus.connect()
    await _restore_snapshot()
    global _heartbeat
    if _bus.embedded is not None:
        _heartbeat = HeartbeatService(
            _bus.embedded, [cfg["id"] for cfg in _cache.config]
        )
        logger.info("heartbeat service ready (%d signals)", len(_cache.config))
    _snapshot_store.start()
    _values_feed.start()
    logger.info("OPC UA bus started in %s mode", _bus.mode)


@app.on_event("shutdown")
async def shutdown() -> None:
    await _values_feed.stop()
    await _snapshot_store.stop()
    await _station_proxy.aclose()
    engine = get_scenario_engine()
    if engine is not None:
        await engine.stop()
    if _heartbeat is not None:
        await _heartbeat.stop()
    # Штатный останов — последний снимок перед закрытием шины (§13.2).
    _snapshot_store.save()
    await _bus.close()


async def _signals_snapshot() -> dict:
    """Снимок всех карточек: один и тот же для REST и WS (ТЗ #5 §12)."""
    from typing import Any, Dict

    signals: Dict[str, Any] = {}
    for cfg in _cache.config:
        state = _cache.get_state(cfg["id"])
        card = state.card() if state is not None else {}
        owner = _ownership.owner_of(cfg["id"])
        signals[cfg["id"]] = {
            "id": cfg["id"],
            "short_id": cfg["id"],
            "name": cfg.get("name", cfg["id"]),
            "type": cfg.get("type", "Boolean"),
            "writable": cfg.get("writable", False),
            "value": await _bus.read_value(cfg["id"]),
            "revision": card.get("revision", 0),
            "source": card.get("source"),
            "changed_at": card.get("changed_at"),
            # Владение (ТЗ #5 §9.2): кто держит сигнал сейчас, если держит.
            "owner": owner.title if owner is not None else None,
            "owner_id": owner.id if owner is not None else None,
            "direction": cfg.get("direction"),
            "channel": cfg.get("channel"),
            "module": cfg.get("module"),
            "cabinet": cfg.get("cabinet"),
            "device": cfg.get("device"),
            "address": cfg.get("address") or cfg.get("node_id") or cfg["id"],
            "project_tag": cfg.get("project_tag") or cfg.get("address") or cfg["id"],
            "bit": cfg.get("bit"),
            "word_address": cfg.get("word_address"),
            "logic": cfg.get("logic"),
        }
    return {"signals": signals}


@app.get("/api/signals")
async def get_signals() -> dict:
    return await _signals_snapshot()


class WriteSignalRequest(BaseModel):
    id: str
    value: Any = None
    # ТЗ #5 §6: идемпотентность (повтор с тем же op_id не выполняется дважды)
    # и оптимистичная блокировка (ревизия на момент чтения клиентом).
    op_id: Optional[str] = None
    expected_revision: Optional[int] = None


class WriteBatchRequest(BaseModel):
    values: dict[str, Any] = {}
    op_id: Optional[str] = None
    # §8: защита ручной записи от устаревшего снимка — ревизия на момент
    # чтения клиентом; конфликт отклоняет пакет, значение не перезаписывается
    # незаметно.
    expected_revisions: Optional[dict[str, int]] = None


_STATUS_TO_HTTP = {
    "unknown_signal": 404,
    "not_writable": 403,
    "invalid_value": 400,
    "revision_conflict": 409,
    "owned": 409,
    "write_failed": 502,
}


def _record_operation(
    result: dict[str, Any], signals: List[str], elapsed_s: float, source: str
) -> None:
    """Операция применения — в журнал диагностики (§14); отказ тоже событие."""
    revisions = {
        signal_id: result["results"][signal_id]["revision"]
        for signal_id in result["applied"]
        if "revision" in result["results"][signal_id]
    }
    _diagnostics.record_operation(
        op_id=str(result.get("op_id") or "-"),
        source=source,
        status=str(result.get("status")),
        signals=signals,
        errors=list(result.get("errors") or []),
        duration_ms=elapsed_s * 1000.0,
        revisions=revisions,
    )


def _reject_by_code(res: dict[str, Any], signal_id: str) -> HTTPException:
    """Ошибка проверки/записи одной команды → HTTP-статус."""
    code = res.get("code")
    if code == "unknown_signal":
        return HTTPException(status_code=404, detail=f"Сигнал {signal_id} не найден")
    if code == "not_writable":
        return HTTPException(status_code=403, detail=f"Сигнал {signal_id} только для чтения")
    if code == "owned":
        return HTTPException(status_code=409, detail=res.get("error", "Сигнал под владением сценария"))
    if code == "revision_conflict":
        return HTTPException(status_code=409, detail=res.get("error", "конфликт ревизии"))
    if code == "write_failed":
        return HTTPException(status_code=502, detail="Ошибка записи сигнала")
    return HTTPException(status_code=400, detail=res.get("error", "Некорректное значение"))


@app.post("/api/signals/write")
async def write_signal(req: WriteSignalRequest) -> dict:
    # Сценарий исключён из грубой блокировки: его сигналы защищает владение
    # по-сигнально (код owned → 409), остальные ручная правка менять может.
    ensure_manual_writes_allowed(exclude={"scenario"})
    expected = {req.id: req.expected_revision} if req.expected_revision is not None else None
    started = time.monotonic()
    result = await _bus.apply_changes(
        {req.id: req.value},
        source="manual",
        op_id=req.op_id,
        expected_revisions=expected,
        require_writable=True,
        contract="api",
    )
    _record_operation(result, [req.id], time.monotonic() - started, "manual")
    if result["status"] == "conflict":
        raise HTTPException(status_code=409, detail=result["error"])
    res = result["results"].get(req.id, {})
    if res.get("status") in ("rejected", "not_applied", "failed"):
        raise _reject_by_code(res, req.id)
    # §13.2: после успешной команды — снимок завершённой границы применения.
    _snapshot_store.save()
    body = {
        "status": "ok",
        "id": req.id,
        "value": res.get("value"),
        "revision": res.get("revision"),
        "op_id": result["op_id"],
        "source": "manual",
    }
    if result.get("replayed"):
        body["replayed"] = True
    return body


@app.post("/api/signals/write_batch")
async def write_batch(req: WriteBatchRequest) -> dict:
    ensure_manual_writes_allowed(exclude={"scenario"})
    # §7: пакет проверяется целиком до записи; ошибка проверки отклоняет
    # пакет, значения не меняются. Ответ честный: применённые, ошибки,
    # результат по каждому сигналу.
    started = time.monotonic()
    result = await _bus.apply_changes(
        req.values,
        source="manual_batch",
        op_id=req.op_id,
        expected_revisions=req.expected_revisions,
        require_writable=True,
        contract="api",
    )
    _record_operation(result, list(req.values), time.monotonic() - started, "manual_batch")
    if result["status"] == "conflict":
        raise HTTPException(status_code=409, detail=result["error"])
    now = datetime.now(timezone.utc)
    for signal_id in result["applied"]:
        _ledger.note(now, signal_id, result["results"][signal_id]["value"])
    if result["applied"]:
        _snapshot_store.save()
    body = {
        "status": result["status"],
        "applied": result["applied"],
        "errors": result["errors"],
        "results": result["results"],
        "op_id": result["op_id"],
    }
    if result.get("replayed"):
        body["replayed"] = True
    return body


class ResetRequest(BaseModel):
    """Сброс к начальным значениям реестра (ТЗ #5 §8).

    Без списка — весь разрешённый (записываемый) набор; со списком —
    выбранный набор. Неизвестный идентификатор — честный отказ.
    """

    signal_ids: Optional[List[str]] = None
    op_id: Optional[str] = None


@app.post("/api/signals/reset")
async def reset_signals(req: ResetRequest) -> dict:
    """Сброс значений через общий механизм применения (ТЗ #5 §8).

    Начальные значения берутся из реестра (поле ``default``, иначе —
    значение типа). Те же проверки и правила, что у ручной записи: тип,
    право на запись, владение (занятый сценарием сигнал отклонит сброс
    кодом ``owned``), дедупликация op_id. Таймлайн не затрагивается:
    позиция не меняется, тренд-файл не читается.
    """
    ensure_manual_writes_allowed(exclude={"scenario"})
    known = {cfg["id"]: cfg for cfg in _cache.config}
    if req.signal_ids is not None:
        unknown = [signal_id for signal_id in req.signal_ids if signal_id not in known]
        if unknown:
            raise HTTPException(
                status_code=404,
                detail=f"Сигнал(ы) не найдены: {', '.join(unknown)}",
            )
        chosen: List[str] = list(req.signal_ids)
    else:
        chosen = [cfg["id"] for cfg in _cache.config if cfg.get("writable", False)]
    values: dict[str, Any] = {}
    for signal_id in chosen:
        cfg = known[signal_id]
        default = cfg.get("default")
        if default is None:
            default = signal_types.default_for(cfg.get("type") or "Boolean")
        values[signal_id] = default
    started = time.monotonic()
    result = await _bus.apply_changes(
        values,
        source="reset",
        op_id=req.op_id,
        require_writable=True,
        contract="api",
    )
    _record_operation(result, chosen, time.monotonic() - started, "reset")
    if result["status"] == "conflict":
        raise HTTPException(status_code=409, detail=result["error"])
    now = datetime.now(timezone.utc)
    for signal_id in result["applied"]:
        _ledger.note(now, signal_id, result["results"][signal_id]["value"])
    if result["applied"]:
        _snapshot_store.save()
    body = {
        "status": result["status"],
        "applied": result["applied"],
        "errors": result["errors"],
        "results": result["results"],
        "op_id": result["op_id"],
    }
    if result.get("replayed"):
        body["replayed"] = True
    return body


class HeartbeatRequest(BaseModel):
    enabled: bool


@app.get("/api/heartbeat")
async def get_heartbeat() -> dict:
    """Текущее состояние heartbeat-режима (cyclic refresh SourceTimestamp)."""
    if _heartbeat is None:
        return {"enabled": False, "supported": False}
    return {"enabled": _heartbeat.enabled, "supported": True}


@app.put("/api/heartbeat")
async def put_heartbeat(req: HeartbeatRequest) -> dict:
    """Включить/выключить heartbeat-режим (доступен только при OPC_UA_MODE=server)."""
    if _heartbeat is None:
        raise HTTPException(
            status_code=409,
            detail="Режим heartbeat доступен только при OPC_UA_MODE=server",
        )
    await _heartbeat.set_enabled(req.enabled)
    return {"enabled": _heartbeat.enabled, "supported": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # §12: подключение начинается со снимка — между REST-снимком и потоком
    # дельт нет незамеченного пропуска: клиент сверяет seq. Снимок строится
    # до accept: тик между снимком и подпиской уйдёт мимо и проявится
    # пропуском seq, по которому клиент перезапросит снимок.
    snapshot = await _signals_snapshot()
    snapshot["type"] = "signals_snapshot"
    snapshot["seq"] = _values_feed.seq
    await manager.connect(websocket)
    try:
        await websocket.send_text(json.dumps(snapshot, ensure_ascii=False))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


def _serve_frontend(app: FastAPI) -> None:
    dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/")
        async def index():
            from fastapi.responses import FileResponse
            return FileResponse(str(dist / "index.html"))


_serve_frontend(app)
