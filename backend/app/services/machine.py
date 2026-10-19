"""Пульт машины: исходное состояние, одно активное воздействие, факты по сигналам.

Отличие от сценариев: сценарий проигрывает заранее собранный цикл, а пульт
держит выбранное состояние и принимает ОДНО воздействие за раз. Состояния
взяты из реальных записей телеметрии (``recordings/<mode>.gz``, кадр t=0):
отключённая машина, заряженная и стоящая, машина в движении. Остальные
опубликованные сигналы берутся из ``BASELINE`` модели — иначе состояние
описывало бы 20 сигналов из 101.

Воздействия собраны теми же цепочками защиты, что эпизоды сценариев
(``hoist._safety_stop``, ``hoist._decelerate``, ``hoist._acknowledge_and_reset``):
сначала причина (вход), затем её последствия (отпадание реле ТП, снятие
привода, выбег, действия машиниста). Имитатор программы ПЛК не исполняет,
поэтому последствия заданы здесь явно — новый эпизод сверяется с цепочкой
защиты проекта, а не выдумывается.

Что показывать оператору, решают ФАКТИЧЕСКИЕ значения сигналов, а не факт
нажатия кнопки: ``status()`` читает скорость, реле ТП, цепь стопа и ошибку
привода из шины и выводит из них строку состояния.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .ownership import Owner, OwnershipRegistry
from .scenario.hoist import (
    BASELINE,
    DRIVE_EMERGENCY_OFF,
    ENCODERS,
    ESTOP_CONSOLE,
    ESTOP_CONSOLE_2,
    INV_FAULT,
    INV_FAULT_ID,
    INV_FUNCTION,
    INV_NORMAL_OPER,
    INV_STATUS,
    INV_STATUS_1,
    INV_STATUS_2,
    KAR_DOWN,
    KAR_UP,
    KAR_ZERO,
    KRT_BRAKED,
    KRT_RELEASED,
    MOTOR_SPEED,
    SW1_ALARM,
    SW1_FAULT,
    SW1_RELEASE_HOLD_BRAKE,
    SW1_ROTATION_DIR,
    SW1_XFUNCTION,
    SW2_RAMP_ACTIVE,
    TP_RELAY,
    HoistState,
    _acknowledge_and_reset,
    _decelerate,
    _safety_stop,
)
from .scenario.recorded_modes import RecordedMode, load_recorded_modes
from .scenario.timeline import TICK_MS, Change, Timeline

logger = logging.getLogger("opcua-imitator.machine")

Writer = Callable[[str, Any, datetime], Awaitable[bool]]
WriterFactory = Callable[[str], Writer]
Reader = Callable[[str], Awaitable[Any]]

#: Слово состояния инвертора: бит «управление от ПЛК/напряжение в норме».
#: В реальных записях он отличает обесточенную машину (0x0001) от готовой
#: (0x0401) и идущей (0x0422) — проверено на кадрах t=0 всех трёх записей.
INV_POWER_READY = 1 << 10

#: Имя события, повтор которого имеет смысл (кратковременный разрыв).
MOMENTARY_KIND = "estop_glitch"


class MachineError(Exception):
    """Отказ пульта с машинным кодом для HTTP-ответа."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class StateSpec:
    """Исходное состояние машины: запись-источник и уточнения кадра."""

    kind: str
    title: str
    hint: str
    mode: str
    overrides: Mapping[str, Any] = field(default_factory=dict)


#: «Заряжена» — специальное технологическое состояние (предохранительный
#: тормоз заряжен, кнопка S21), поэтому подпись оставлена доменной, а не
#: заменена на «Готова к работе».
STATES: Dict[str, StateSpec] = {
    "off": StateSpec(
        kind="off",
        title="Отключена",
        hint="Машина выключена",
        mode="machine_off",
    ),
    "ready": StateSpec(
        kind="ready",
        title="Заряжена, стоит",
        hint="Предохранительный тормоз заряжен, привод готов",
        mode="charged_stopped",
    ),
    "moving": StateSpec(
        kind="moving",
        title="В движении",
        hint="Штатный ход без аварий",
        mode="in_motion",
        # Запись публикует только привод и датчики канала 2; состояние
        # машиниста (КАР ушла из нуля, КРТ расторможен) выводим из движения,
        # иначе кадр противоречил бы сам себе: скорость есть, а рукоятка в нуле.
        overrides={
            KRT_BRAKED: False,
            KRT_RELEASED: True,
            KAR_ZERO: False,
            KAR_DOWN: True,
        },
    ),
}


@dataclass(frozen=True)
class EventSpec:
    """Воздействие: причина, её последствия и чем её устраняют."""

    kind: str
    title: str
    group: str  # 'normal' | 'fault'
    requires_motion: bool
    latched: bool
    active_text: str = ""
    done_text: str = ""
    remedy: Optional[str] = None


EVENTS: Dict[str, EventSpec] = {
    "normal_stop": EventSpec(
        kind="normal_stop",
        title="Остановить штатно",
        group="normal",
        requires_motion=True,
        latched=False,
        done_text="Машина остановлена штатно, защиты не срабатывали.",
    ),
    "estop_press": EventSpec(
        kind="estop_press",
        title="Нажать аварийную кнопку",
        group="fault",
        requires_motion=False,
        latched=True,
        active_text="Аварийная кнопка нажата",
        remedy="Отпустить аварийную кнопку",
    ),
    "estop_glitch": EventSpec(
        kind="estop_glitch",
        title="Кратковременно разорвать цепь стопа",
        group="fault",
        requires_motion=True,
        latched=False,
        done_text="Кратковременный разрыв выполнен. Цепь восстановлена.",
    ),
    "drive_emergency_off": EventSpec(
        kind="drive_emergency_off",
        title="Отключить ПЧ аварийно",
        group="fault",
        requires_motion=False,
        latched=True,
        active_text="Питание ПЧ снято аварийной кнопкой",
        remedy="Вернуть питание ПЧ",
    ),
    "inverter_fault": EventSpec(
        kind="inverter_fault",
        title="Вызвать аварию инвертора",
        group="fault",
        requires_motion=True,
        latched=True,
        active_text="Авария инвертора",
        remedy="Квитировать аварию привода",
    ),
    "tp_relay_dropout": EventSpec(
        kind="tp_relay_dropout",
        title="Отключить реле ТП",
        group="fault",
        requires_motion=True,
        latched=True,
        active_text="Реле ТП снято",
        remedy="Взвести реле ТП",
    ),
}


@dataclass
class _Session:
    """Один тест: выбранное состояние, воздействия и история."""

    id: str
    state: str
    owner: Owner
    phase: str = "running"
    started_at: datetime = field(default_factory=_utc_now)
    active_event: Optional[Dict[str, Any]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    notice: Optional[Dict[str, Any]] = None
    busy: bool = False
    error: Optional[str] = None

    def note(self, kind: str, text: str, at: datetime) -> Dict[str, Any]:
        entry = {"at": _iso(at), "kind": kind, "text": text}
        self.history.append(entry)
        if len(self.history) > 200:
            del self.history[:-200]
        return entry


class MachineSession:
    """Пульт машины: состояния из записей, воздействия из цепочек защиты."""

    def __init__(
        self,
        writer_factory: WriterFactory,
        configs: Iterable[Mapping[str, Any]],
        *,
        read_value: Reader,
        ownership: Optional[OwnershipRegistry] = None,
        recorded_dir: Optional[Path] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._writer_factory = writer_factory
        self._configs: Dict[str, Mapping[str, Any]] = {cfg["id"]: cfg for cfg in configs}
        self._known: Tuple[str, ...] = tuple(self._configs)
        self._read = read_value
        self._ownership = ownership
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._session: Optional[_Session] = None
        self._lock = asyncio.Lock()
        self._recorded: Dict[str, RecordedMode] = (
            load_recorded_modes(recorded_dir, config=self._configs.values())
            if recorded_dir is not None
            else load_recorded_modes(config=self._configs.values())
        )

    # -- чтение -------------------------------------------------------------

    @property
    def running(self) -> bool:
        """Драйвер активен, пока тест не завершён (значения под владением)."""
        return self._session is not None and self._session.phase != "finished"

    @property
    def phase(self) -> str:
        return self._session.phase if self._session is not None else "idle"

    def _state_frame(self, state: str) -> Dict[str, Any]:
        """Кадр состояния: BASELINE всей установки + реальные значения записи."""
        spec = STATES[state]
        frame: Dict[str, Any] = {
            signal_id: value for signal_id, value in BASELINE.items() if signal_id in self._configs
        }
        mode = self._recorded.get(spec.mode)
        if mode is not None:
            frame.update({sid: value for sid, value in mode.initial.items() if sid in self._configs})
        frame.update({sid: value for sid, value in spec.overrides.items() if sid in self._configs})
        return frame

    def _declared_signals(self) -> List[str]:
        """Набор под владением: всё, что пульт может изменить."""
        return sorted(set(BASELINE) & set(self._known))

    async def _observed(self) -> Dict[str, Any]:
        """Показания, по которым выводится состояние машины."""
        speed = int(await self._read(MOTOR_SPEED) or 0)
        tp_relay = bool(await self._read(TP_RELAY))
        estop = bool(await self._read(ESTOP_CONSOLE))
        drive_word = int(await self._read(INV_STATUS) or 0)
        fault_id = int(await self._read(INV_FAULT_ID) or 0)
        drive_off = bool(await self._read(DRIVE_EMERGENCY_OFF))
        fault = bool(drive_word & INV_FAULT) or fault_id != 0
        return {
            "speed": speed,
            "tp_relay": tp_relay,
            "estop_circuit": estop,
            "drive_fault": fault,
            "fault_id": fault_id,
            "drive_ready": bool(drive_word & INV_POWER_READY),
            "drive_emergency_ok": drive_off,
        }

    def _verdict(self, observed: Mapping[str, Any]) -> Tuple[str, str]:
        """Строка состояния и пояснение — только из фактических сигналов."""
        speed = int(observed["speed"])
        if speed != 0:
            headline = "Машина в движении"
            detail = f"Скорость {abs(speed)}; реле ТП {'под током' if observed['tp_relay'] else 'снято'}"
        elif not observed["drive_ready"]:
            headline = "Машина отключена"
            detail = "Привод не готов к работе, управление снято"
        elif observed["drive_fault"]:
            headline = "Машина остановлена"
            detail = f"Авария привода, код {observed['fault_id']}"
        elif not observed["estop_circuit"]:
            headline = "Машина остановлена"
            detail = "Цепь аварийного останова разомкнута"
        elif not observed["drive_emergency_ok"]:
            headline = "Машина остановлена"
            detail = "Питание ПЧ снято аварийной кнопкой"
        elif not observed["tp_relay"]:
            headline = "Машина остановлена"
            detail = "Реле ТП снято: защита сработала, тормоз наложен"
        else:
            headline = "Машина стоит"
            detail = "Привод готов, реле ТП под током, защита в норме"
        return headline, detail

    def _events_hint(self, session: Optional[_Session], observed: Optional[Mapping[str, Any]]) -> Optional[str]:
        if session is None or session.phase == "idle":
            return "Сначала выберите состояние и нажмите «Запустить»"
        if session.phase == "finished":
            return "Тест завершён: новые события не применяются"
        if session.busy:
            return "Выполняется воздействие — дождитесь завершения"
        if session.active_event is not None:
            return "Воздействие активно: сначала устраните его"
        if session.state == "off":
            return "Машина отключена: выберите другое состояние и нажмите «Запустить»"
        if observed is not None and int(observed["speed"]) == 0:
            return "Машина стоит: события, которым нужен ход, недоступны — выберите «В движении» и запустите заново"
        return None

    def _event_availability(
        self, spec: EventSpec, session: Optional[_Session], observed: Mapping[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        if session is None or session.phase == "idle":
            return False, "Сначала выберите состояние и нажмите «Запустить»"
        if session.phase == "finished":
            return False, "Тест завершён: новые события не применяются"
        if session.busy:
            return False, "Выполняется воздействие — дождитесь завершения"
        if session.active_event is not None:
            return False, "Сначала устраните активное воздействие"
        if session.state == "off":
            return False, "Машина отключена: воздействия недоступны"
        if spec.requires_motion and int(observed["speed"]) == 0:
            return False, "Нужен ход машины: выберите «В движении» и запустите заново"
        return True, None

    async def status(self) -> Dict[str, Any]:
        session = self._session
        observed = await self._observed()
        headline, detail = self._verdict(observed)
        events = []
        for spec in EVENTS.values():
            available, reason = self._event_availability(spec, session, observed)
            events.append(
                {
                    "kind": spec.kind,
                    "title": spec.title,
                    "group": spec.group,
                    "requires_motion": spec.requires_motion,
                    "available": available,
                    "reason": reason,
                    "remedy": spec.remedy,
                }
            )
        active = session.active_event if session is not None else None
        last_action = None
        if session is not None:
            for entry in reversed(session.history):
                if entry["kind"] != "start":
                    last_action = entry
                    break
        return {
            "available": True,
            "phase": session.phase if session is not None else "idle",
            "session_id": session.id if session is not None else None,
            "state": session.state if session is not None else None,
            "states": [
                {
                    "kind": spec.kind,
                    "title": spec.title,
                    "hint": spec.hint,
                    "selected": session is not None and session.state == spec.kind,
                }
                for spec in STATES.values()
            ],
            "events": events,
            "events_hint": self._events_hint(session, observed),
            "active_event": active,
            "busy": bool(session.busy) if session is not None else False,
            "notice": session.notice if session is not None else None,
            "observed": {
                "speed": observed["speed"],
                "speed_text": f"{abs(int(observed['speed']))}",
                "tp_relay": observed["tp_relay"],
                "tp_relay_text": "под током" if observed["tp_relay"] else "снято",
                "estop_circuit": observed["estop_circuit"],
                "estop_circuit_text": "замкнута" if observed["estop_circuit"] else "разомкнута",
                "drive_fault": observed["drive_fault"],
                "fault_id": observed["fault_id"],
                "drive_fault_text": (
                    f"код {observed['fault_id']}" if observed["drive_fault"] else "нет"
                ),
                "drive_ready": observed["drive_ready"],
            },
            "headline": headline,
            "detail": detail,
            "last_action": last_action,
            "history": list(reversed(session.history)) if session is not None else [],
            "can_start": session is None or session.phase == "finished",
            "can_finish": session is not None and session.phase in ("running", "event"),
            "can_restart": session is not None and session.phase != "idle",
        }

    # -- запись -------------------------------------------------------------

    async def _write(self, session: _Session, writer: Writer, signal_id: str, value: Any, when: datetime) -> None:
        if signal_id not in self._configs:
            return
        ok = await writer(signal_id, value, when)
        if not ok:
            logger.warning("machine %s: запись %s отклонена", session.id, signal_id)

    async def _write_frame(self, session: _Session, writer: Writer, frame: Mapping[str, Any], when: datetime) -> None:
        for signal_id, value in frame.items():
            await self._write(session, writer, signal_id, value, when)

    async def _replay(self, session: _Session, writer: Writer, changes: Sequence[Change], base: datetime) -> None:
        """Проиграть изменения в темпе шкалы; моменты — относительно ``base``."""
        started = self._monotonic()
        for at_ms, group in itertools.groupby(changes, key=lambda change: change.at_ms):
            delay = at_ms / 1000.0 - (self._monotonic() - started)
            if delay > 0:
                await self._sleep(delay)
            when = base + timedelta(milliseconds=at_ms)
            for change in group:
                await self._write(session, writer, change.signal_id, change.value, when)

    async def _current_values(self) -> Dict[str, Any]:
        return {signal_id: await self._read(signal_id) for signal_id in self._known}

    # -- управление ---------------------------------------------------------

    async def start(self, state: str) -> Dict[str, Any]:
        """Запустить машину в выбранном состоянии: кадр состояния целиком."""
        spec = STATES.get(state)
        if spec is None:
            raise MachineError("unknown_state", f"Неизвестное состояние: {state}")
        async with self._lock:
            session = self._session
            if session is not None and session.phase in ("running", "event"):
                raise MachineError(
                    "already_started",
                    "Тест уже идёт: «Начать заново» с новым состоянием или «Завершить тест»",
                )
            new = self._open(state, spec)
            writer = self._writer_factory(new.id)
            try:
                await self._write_frame(new, writer, self._state_frame(state), self._clock())
            except Exception:
                self._release(new)
                raise
            self._session = new
            new.note("start", f"машина запущена: {spec.title.lower()}", self._clock())
            logger.info("machine session %s started: state=%s", new.id, state)
            return await self.status()

    def _open(self, state: str, spec: StateSpec) -> _Session:
        session_id = uuid.uuid4().hex[:12]
        owner = Owner(
            id=f"machine:{session_id}",
            kind="machine",
            title=f"Пульт машины — {spec.title.lower()}",
        )
        signals = self._declared_signals()
        if self._ownership is not None:
            conflicts = self._ownership.acquire(owner, signals)
            if conflicts:
                listing = "; ".join(f"{c.signal_id} — «{c.owner.title}»" for c in conflicts)
                raise MachineError(
                    "signals_busy",
                    f"Запуск отклонён: сигналы под владением другого прогона ({listing})",
                )
        return _Session(id=session_id, state=state, owner=owner)

    def _release(self, session: _Session) -> None:
        if self._ownership is not None:
            self._ownership.release(session.owner.id)

    async def restart(self, state: Optional[str] = None) -> Dict[str, Any]:
        """Начать заново: очистить воздействия и историю, вернуть исходное состояние.

        ``state`` — новое выбранное состояние: смена карточки применяется
        именно здесь, явным новым запуском, а не в момент клика.
        """
        if state is not None and state not in STATES:
            raise MachineError("unknown_state", f"Неизвестное состояние: {state}")
        async with self._lock:
            session = self._session
            if session is None:
                raise MachineError("not_started", "Тест ещё не запускался: выберите состояние и нажмите «Запустить»")
            if session.busy:
                raise MachineError("busy", "Выполняется воздействие — дождитесь завершения")
            target = state or session.state
            # После «Завершить тест» владение снято: повторный запуск обязан
            # вернуть его себе, иначе пульт писал бы без защиты от чужого прогона.
            if self._ownership is not None:
                conflicts = self._ownership.acquire(session.owner, self._declared_signals())
                if conflicts:
                    listing = "; ".join(
                        f"{c.signal_id} — «{c.owner.title}»" for c in conflicts
                    )
                    raise MachineError(
                        "signals_busy",
                        f"Запуск отклонён: сигналы под владением другого прогона ({listing})",
                    )
            writer = self._writer_factory(session.id)
            await self._write_frame(session, writer, self._state_frame(target), self._clock())
            session.state = target
            session.phase = "running"
            session.active_event = None
            session.notice = None
            session.error = None
            session.history.clear()
            session.note("start", f"тест начат заново: {STATES[target].title.lower()}", self._clock())
            logger.info("machine session %s restarted: state=%s", session.id, target)
            return await self.status()

    async def finish(self) -> Dict[str, Any]:
        """Завершить тест: значения остаются, новые воздействия не принимаются."""
        async with self._lock:
            session = self._session
            if session is None:
                raise MachineError("not_started", "Тест ещё не запускался")
            if session.busy:
                raise MachineError("busy", "Выполняется воздействие — дождитесь завершения")
            session.phase = "finished"
            session.note("finish", "тест завершён, значения оставлены для просмотра", self._clock())
            self._release(session)
            logger.info("machine session %s finished", session.id)
            return await self.status()

    async def event(self, kind: str) -> Dict[str, Any]:
        """Применить воздействие: причина и цепочка последствий на сигналах."""
        spec = EVENTS.get(kind)
        if spec is None:
            raise MachineError("unknown_event", f"Неизвестное воздействие: {kind}")
        async with self._lock:
            session = self._session
            if session is None:
                raise MachineError("not_started", "Сначала запустите машину")
            if session.phase == "finished":
                raise MachineError("finished", "Тест завершён: новые события не применяются")
            if session.busy:
                raise MachineError("busy", "Выполняется воздействие — дождитесь завершения")
            if session.active_event is not None:
                raise MachineError(
                    "event_active", "Сначала устраните активное воздействие"
                )
            observed = await self._observed()
            available, reason = self._event_availability(spec, session, observed)
            if not available:
                raise MachineError("not_applicable", reason or "Воздействие недоступно")
            session.busy = True
            session.notice = None
            writer = self._writer_factory(session.id)
            base = self._clock()
            try:
                current = await self._current_values()
                changes = self._build_event(kind, current)
                session.note(kind, spec.active_text or spec.done_text, base)
                await self._replay(session, writer, changes, base)
                if spec.latched:
                    session.active_event = {
                        "kind": spec.kind,
                        "title": spec.active_text or spec.title,
                        "since": _iso(base),
                        "remedy": spec.remedy,
                    }
                else:
                    session.notice = {
                        "kind": "ok",
                        "text": spec.done_text,
                        "repeat_event": kind if kind == MOMENTARY_KIND else None,
                    }
                session.phase = "running"
            finally:
                session.busy = False
            logger.info("machine session %s event %s applied", session.id, kind)
            return await self.status()

    async def remedy(self) -> Dict[str, Any]:
        """Устранить активное воздействие: восстановить причину и взвести ТП."""
        async with self._lock:
            session = self._session
            if session is None:
                raise MachineError("not_started", "Тест не запущен")
            if session.busy:
                raise MachineError("busy", "Выполняется воздействие — дождитесь завершения")
            if session.phase == "finished":
                raise MachineError("finished", "Тест завершён: воздействия не применяются")
            active = session.active_event
            if active is None:
                raise MachineError("no_active_event", "Активного воздействия нет")
            kind = active["kind"]
            spec = EVENTS[kind]
            writer = self._writer_factory(session.id)
            base = self._clock()
            session.busy = True
            session.notice = None
            try:
                current = await self._current_values()
                changes = self._build_remedy(kind, current)
                await self._replay(session, writer, changes, base)
                session.active_event = None
                session.notice = {
                    "kind": "ok",
                    "text": self._remedy_text(kind),
                    "repeat_event": None,
                }
                session.note(kind, self._remedy_text(kind), base)
            finally:
                session.busy = False
            logger.info("machine session %s remedy for %s applied", session.id, kind)
            return await self.status()

    @staticmethod
    def _remedy_text(kind: str) -> str:
        return {
            "estop_press": "аварийная кнопка отпущена, реле ТП взведено",
            "drive_emergency_off": "питание ПЧ возвращено, реле ТП взведено",
            "inverter_fault": "авария привода квитирована, реле ТП взведено",
            "tp_relay_dropout": "реле ТП взведено, защита в норме",
        }.get(kind, "воздействие устранено")

    # -- сборка изменений ---------------------------------------------------

    def _plant(self, current: Mapping[str, Any]) -> HoistState:
        return HoistState(
            left_at_top=bool(current.get("ch1-10a3-sq1", True)),
            encoders=[int(current.get(enc) or 0) for enc in ENCODERS],
        )

    @staticmethod
    def _direction(current: Mapping[str, Any]) -> int:
        speed = int(current.get(MOTOR_SPEED) or 0)
        if speed != 0:
            return -1 if speed < 0 else 1
        return -1 if current.get(KAR_UP) else 1

    def _build_event(self, kind: str, current: Mapping[str, Any]) -> Tuple[Change, ...]:
        """Собрать шкалу воздействия из текущего состояния сигналов."""
        tl = Timeline(current, self._known)
        plant = self._plant(current)
        direction = self._direction(current)
        rng = random.Random(0)
        if kind == "normal_stop":
            tl.set(0, KAR_UP if direction > 0 else KAR_DOWN, False)
            tl.set(0, KAR_ZERO, True)
            t = _decelerate(tl, plant, 0, direction, rng)
            tl.set_bits(t + 300, INV_STATUS, INV_FUNCTION | INV_NORMAL_OPER, False)
            tl.set_bits(t + 300, INV_STATUS_1, SW1_XFUNCTION | SW1_ROTATION_DIR, False)
            tl.set_bits(t + 300, INV_STATUS_2, SW2_RAMP_ACTIVE, False)
            tl.set(t + 800, KRT_BRAKED, True)
            tl.set(t + 800, KRT_RELEASED, False)
            tl.set_bits(t + 800, INV_STATUS_1, SW1_RELEASE_HOLD_BRAKE, False)
        elif kind == "estop_press":
            tl.set(0, ESTOP_CONSOLE, False)
            tl.set(0, ESTOP_CONSOLE_2, False)
            _safety_stop(tl, plant, 0, direction, relay_is_cause=False)
        elif kind == "estop_glitch":
            tl.set(0, ESTOP_CONSOLE, False)
            tl.set(300, ESTOP_CONSOLE, True)
            _safety_stop(tl, plant, 0, direction, relay_is_cause=False)
        elif kind == "drive_emergency_off":
            tl.set(0, DRIVE_EMERGENCY_OFF, False)
            _safety_stop(tl, plant, 0, direction, relay_is_cause=False)
        elif kind == "inverter_fault":
            tl.set_bits(0, INV_STATUS, INV_FAULT, True)
            tl.set_bits(0, INV_STATUS_1, SW1_FAULT | SW1_ALARM, True)
            tl.set(0, INV_FAULT_ID, 7)
            _safety_stop(tl, plant, 0, direction, relay_is_cause=False)
        elif kind == "tp_relay_dropout":
            tl.set(0, TP_RELAY, False)
            _safety_stop(tl, plant, 0, direction, relay_is_cause=True)
        else:  # pragma: no cover — EVENTS перечисляет те же ветки
            raise MachineError("unknown_event", f"Неизвестное воздействие: {kind}")
        return tl.changes()

    def _build_remedy(self, kind: str, current: Mapping[str, Any]) -> Tuple[Change, ...]:
        """Снять причину и вернуть защиту: квитирование и заряд ТП."""
        tl = Timeline(current, self._known)
        if kind == "estop_press":
            tl.set(0, ESTOP_CONSOLE, True)
            tl.set(0, ESTOP_CONSOLE_2, True)
            _acknowledge_and_reset(tl, 500)
        elif kind == "drive_emergency_off":
            tl.set(0, DRIVE_EMERGENCY_OFF, True)
            _acknowledge_and_reset(tl, 500)
        elif kind == "inverter_fault":
            _acknowledge_and_reset(tl, 0)
        elif kind == "tp_relay_dropout":
            _acknowledge_and_reset(tl, 0)
        else:
            raise MachineError("no_active_event", "У этого воздействия нет действия устранения")
        return tl.changes()
