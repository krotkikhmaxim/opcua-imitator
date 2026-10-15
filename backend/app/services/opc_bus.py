import asyncio
import json
import logging
import os
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.services import signal_types
from app.services.ownership import Owner, OwnershipRegistry
from app.services.signal_cache import SignalCache

logger = logging.getLogger("opcua-imitator.bus")

#: Окно дедупликации по op_id (ТЗ #5 §6): последние N применённых команд.
#: Повтор команды с тем же op_id в пределах окна возвращает сохранённый
#: результат и не выполняется повторно; за окном — выполняется как новая.
APPLY_DEDUP_WINDOW = 4096


class OPCMode:
    IMITATOR = "imitator"
    REAL = "real"
    SERVER = "server"


class OPCSignalBus:
    """
    Абстракция чтения/записи сигналов.

    В режиме IMITATOR используется встроенный кэш для генерации/чтения значений.
    В режиме REAL подключается к реальному OPC UA серверу через asyncua.
    В режиме SERVER запускается встроенный OPC UA сервер (asyncua.Server),
    который публикует сигналы как переменные; значения читаются/пишутся через
    его адресное пространство, а кэш остаётся зеркалом для веб-интерфейса.

    Все внутренние изменения состояния идут через ``apply_changes`` (ТЗ #5 §6):
    проверка всего пакета до записи, применение по одному сигналу, результат
    по каждому, подтверждение (карточка состояния) после успешной записи.
    """

    def __init__(self, cache: SignalCache, mode: str = OPCMode.IMITATOR,
                 endpoint: Optional[str] = None, timeout: int = 30,
                 server_endpoint: Optional[str] = None):
        self.cache = cache
        self.mode = mode
        self.endpoint = endpoint or os.getenv("OPC_UA_ENDPOINT", "opc.tcp://localhost:4840")
        self.server_endpoint = server_endpoint or os.getenv(
            "OPC_UA_SERVER_ENDPOINT", "opc.tcp://0.0.0.0:4840"
        )
        self.timeout = timeout
        self._client = None
        self._nodes: Dict[str, Any] = {}
        self._embedded = None
        # Дедупликация команд: op_id -> (отпечаток тела, ответ). OrderedDict —
        # вытеснение старейших за пределами окна.
        self._apply_history: "OrderedDict[str, Tuple[str, Dict[str, Any]]]" = OrderedDict()
        # Одновременные apply_changes выполняются по одному: порядок обработки
        # определён (§6), гонки ревизий внутри пакета нет.
        self._apply_lock = asyncio.Lock()
        # Владение сигналами (ТЗ #5 §9.2): запись в занятый сигнал — только
        # самому владельцу. Устанавливается при старте приложения.
        self._ownership: Optional[OwnershipRegistry] = None

    def set_ownership(self, registry: Optional[OwnershipRegistry]) -> None:
        self._ownership = registry

    @property
    def embedded(self) -> Optional[Any]:
        """Handle встроенного OPC UA сервера (режим SERVER), иначе None."""
        return self._embedded

    async def connect(self) -> None:
        if self.mode == OPCMode.SERVER:
            from app.services.embedded_server import EmbeddedOpcUaServer

            self._embedded = EmbeddedOpcUaServer(self.cache.config, endpoint=self.server_endpoint)
            await self._embedded.start()
            return
        if self.mode == OPCMode.REAL:
            try:
                from asyncua import Client
                self._client = Client(self.endpoint, timeout=self.timeout)
                await self._client.connect()
                ns = self._client.get_namespace_index(uri=os.getenv("OPC_UA_NAMESPACE", ""))
                for cfg in self.cache.config:
                    node_id = cfg.get("node_id")
                    if node_id:
                        self._nodes[cfg["id"]] = self._client.get_node(node_id)
            except Exception as e:
                raise RuntimeError(f"Ошибка подключения к OPC UA: {e}")

    async def read_value(self, signal_id: str) -> Optional[Any]:
        if self.mode == OPCMode.SERVER and self._embedded is not None:
            value = await self._embedded.read_value(signal_id)
            if value is not None:
                self.cache.observe(signal_id, value)
            return value
        if self.mode == OPCMode.IMITATOR:
            return self.cache.get_value(signal_id)
        node = self._nodes.get(signal_id)
        if node is None:
            return None
        try:
            val = await node.read_value()
            self.cache.observe(signal_id, val)
            return val
        except Exception:
            return None

    def _ownership_violation(self, signal_id: str, owner: Optional[str]) -> Optional[str]:
        """Текст отказа, если сигнал занят другим источником (§9.2).

        Писавший без подписи (``owner=None``: ручная, пакетная, восстановление
        состояния) в занятый сигнал не проходит — занятый сигнал меняет только
        его владелец, подписанный «scenario:<run_id>».
        """
        if self._ownership is None:
            return None
        held = self._ownership.owner_of(signal_id)
        if held is not None and held.id != owner:
            return f"Сигнал {signal_id} под владением «{held.title}» ({held.id})"
        return None

    async def write_value(self, signal_id: str, value: Any,
                          source: Optional[str] = None,
                          owner: Optional[str] = None) -> bool:
        """Одиночная запись; ``source`` — имя источника для карточки состояния."""
        violation = self._ownership_violation(signal_id, owner)
        if violation:
            logger.warning("запись отклонена владением: %s", violation)
            return False
        if self.mode == OPCMode.SERVER and self._embedded is not None:
            ok = await self._embedded.write_value(signal_id, value)
            if ok:
                self.cache.confirm(signal_id, value, source=source)
            return ok
        if self.mode == OPCMode.IMITATOR:
            self.cache.confirm(signal_id, value, source=source)
            return True
        node = self._nodes.get(signal_id)
        if node is None:
            return False
        try:
            await node.set_value(value)
            self.cache.confirm(signal_id, value, source=source)
            return True
        except Exception:
            return False

    @property
    def simulated(self) -> bool:
        """Пишет ли шина в собственную модель (встроенный сервер или кэш).

        Сценарии допустимы только тогда: в режиме REAL запись ушла бы во
        внешний сервер, то есть в настоящий ПЛК.
        """
        return self.mode in (OPCMode.SERVER, OPCMode.IMITATOR)

    async def write_value_at(self, signal_id: str, value: Any, when: datetime,
                             source: Optional[str] = None,
                             owner: Optional[str] = None) -> bool:
        """Запись с заданной SourceTimestamp; только для симулированных режимов."""
        violation = self._ownership_violation(signal_id, owner)
        if violation:
            logger.warning("запись отклонена владением: %s", violation)
            return False
        if self.mode == OPCMode.SERVER and self._embedded is not None:
            ok = await self._embedded.write_value_at(signal_id, value, when)
            if ok:
                self.cache.confirm(signal_id, value, source=source)
            return ok
        if self.mode == OPCMode.IMITATOR:
            self.cache.confirm(signal_id, value, source=source)
            return True
        raise RuntimeError("timestamped writes are available only in simulated modes")

    async def apply_changes(
        self,
        changes: Mapping[str, Any],
        *,
        source: str,
        op_id: Optional[str] = None,
        expected_revisions: Optional[Mapping[str, int]] = None,
        require_writable: bool = False,
        contract: str = "api",
        when: Optional[datetime] = None,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Единый механизм применения изменений состояния (ТЗ #5 §6, §7).

        Порядок определён: проверка ВСЕГО пакета до первой записи (существует,
        право на запись, тип, ожидаемая ревизия) — при ошибке проверки пакет
        отклоняется целиком, значения не меняются (§7); затем применение
        по одному сигналу в порядке передачи с результатом по каждому
        (частичное выполнение возможно только от отказа самой записи —
        автоматический откат не выполняется). Подтверждение — после успешной
        записи: карточка состояния (значение, ревизия, источник, время).

        Повтор с тем же ``op_id`` и тем же телом в пределах окна
        ``APPLY_DEDUP_WINDOW`` возвращает сохранённый результат с флагом
        ``replayed`` и не выполняется повторно; тот же ``op_id`` с другим
        телом — конфликт (``status="conflict"``). ``owner`` — личность
        пишущего (ТЗ §9.2): сигнал под владением другого — отказ с кодом
        ``owned``; сам владелец пишет свободно.

        Возвращает ответ команды: ``op_id``, ``status``
        (``applied`` | ``partial`` | ``rejected`` | ``failed`` | ``conflict``),
        ``results[signal_id] = {status, value?, revision?, error?, code?}``,
        ``applied``, ``errors``.
        """
        body = _fingerprint(changes, expected_revisions, require_writable, contract, owner)
        async with self._apply_lock:
            if op_id is not None:
                cached = self._apply_history.get(op_id)
                if cached is not None:
                    seen_body, response = cached
                    if seen_body == body:
                        replayed = dict(response)
                        replayed["replayed"] = True
                        return replayed
                    return {
                        "op_id": op_id,
                        "status": "conflict",
                        "code": "op_id_conflict",
                        "error": "op_id уже использован с другим телом команды",
                        "results": {},
                        "applied": [],
                        "errors": [],
                    }
            if op_id is None:
                op_id = uuid.uuid4().hex

            expected = dict(expected_revisions or {})
            results: Dict[str, Dict[str, Any]] = {}
            errors: List[str] = []
            prepared: List[Tuple[str, Any]] = []
            rejected = False

            # Фаза 1 — проверка всего пакета; ни одной записи ещё не было.
            for signal_id, raw in changes.items():
                cfg = self.cache.get_config(signal_id)
                if cfg is None:
                    errors.append(f"Сигнал {signal_id} не найден")
                    results[signal_id] = {
                        "status": "rejected", "code": "unknown_signal",
                        "error": f"Сигнал {signal_id} не найден",
                    }
                    rejected = True
                    continue
                if require_writable and not cfg.get("writable", False):
                    errors.append(f"Сигнал {signal_id} только для чтения")
                    results[signal_id] = {
                        "status": "rejected", "code": "not_writable",
                        "error": f"Сигнал {signal_id} только для чтения",
                    }
                    rejected = True
                    continue
                try:
                    value = signal_types.coerce(cfg.get("type"), raw, contract=contract)
                except signal_types.SignalValueError as exc:
                    errors.append(f"Сигнал {signal_id}: {exc}")
                    results[signal_id] = {
                        "status": "rejected", "code": "invalid_value",
                        "error": str(exc),
                    }
                    rejected = True
                    continue
                violation = self._ownership_violation(signal_id, owner)
                if violation:
                    errors.append(violation)
                    results[signal_id] = {
                        "status": "rejected", "code": "owned",
                        "error": violation,
                    }
                    rejected = True
                    continue
                wanted = expected.get(signal_id)
                if wanted is not None and self.cache.get_revision(signal_id) != wanted:
                    error = (
                        f"Сигнал {signal_id}: ревизия изменилась "
                        f"(ожидалась {wanted}, текущая {self.cache.get_revision(signal_id)})"
                    )
                    errors.append(error)
                    results[signal_id] = {
                        "status": "rejected", "code": "revision_conflict",
                        "error": error,
                    }
                    rejected = True
                    continue
                prepared.append((signal_id, value))

            if rejected:
                # §7: пакет с ошибкой проверки не применяется вовсе; тот, кто
                # прошёл проверку, помечается «не применён» без причины —
                # причина в отклонённых соседях.
                for signal_id, _ in prepared:
                    results[signal_id] = {
                        "status": "not_applied",
                        "code": "package_rejected",
                        "error": "пакет отклонён целиком",
                    }
                response = {
                    "op_id": op_id, "status": "rejected",
                    "results": results, "applied": [], "errors": errors,
                }
                _remember(self._apply_history, op_id, body, response)
                return response

            # Фаза 2 — применение по одному в порядке передачи; подпись
            # владельца проходит до самой записи (§9.2: владелец пишет свободно).
            applied: List[str] = []
            for signal_id, value in prepared:
                if when is not None:
                    ok = await self.write_value_at(
                        signal_id, value, when, source=source, owner=owner
                    )
                else:
                    ok = await self.write_value(signal_id, value, source=source, owner=owner)
                if ok:
                    revision = self.cache.get_revision(signal_id)
                    results[signal_id] = {
                        "status": "applied", "value": value, "revision": revision,
                    }
                    applied.append(signal_id)
                else:
                    error = f"Ошибка записи сигнала {signal_id}"
                    errors.append(error)
                    results[signal_id] = {
                        "status": "failed", "code": "write_failed", "error": error,
                    }
            if len(applied) == len(prepared):
                status = "applied"
            elif applied:
                status = "partial"  # §7: «частично выполнено», без отката
            else:
                status = "failed"
            response = {
                "op_id": op_id, "status": status,
                "results": results, "applied": applied, "errors": errors,
            }
            _remember(self._apply_history, op_id, body, response)
            return response

    async def close(self) -> None:
        if self._embedded is not None:
            try:
                await self._embedded.stop()
            except Exception:
                pass
            self._embedded = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None


def _fingerprint(
    changes: Mapping[str, Any],
    expected_revisions: Optional[Mapping[str, int]],
    require_writable: bool,
    contract: str,
    owner: Optional[str] = None,
) -> str:
    """Отпечаток тела команды для дедупликации: тот же op_id + другое тело = конфликт."""
    return json.dumps(
        {
            "changes": [[sid, raw] for sid, raw in changes.items()],
            "expected": sorted((expected_revisions or {}).items()),
            "require_writable": require_writable,
            "contract": contract,
            "owner": owner,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _remember(
    history: "OrderedDict[str, Tuple[str, Dict[str, Any]]]",
    op_id: str,
    body: str,
    response: Dict[str, Any],
) -> None:
    history[op_id] = (body, response)
    while len(history) > APPLY_DEDUP_WINDOW:
        history.popitem(last=False)
