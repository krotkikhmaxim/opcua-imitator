"""Движок сценариев: проигрывает циклы установки в реальном времени.

Один движок на приложение, несколько прогонов одновременно (ТЗ #5 §9.2):
непересекающиеся наборы сигналов играют параллельно, пересечение наборов
отклоняет запуск целиком. Сначала пишется исправное состояние, затем цикл за
циклом: штатные рейсы, эпизод (авария или штатная остановка), возврат в
работу. Каждое изменение уходит в адресное пространство с SourceTimestamp
момента сценария — потребитель видит точные интервалы между событиями, даже
если запись чуть запоздала.

Владение (§9.2): прогон до старта объявляет набор сигналов (синтез —
BASELINE, режим записи — свой набор) и держит его исключительно; пауза и
ошибка сохраняют владение, завершение и остановка освобождают. После ошибки
прогон стоит с владением до явного решения оператора.

Журнал правды (что случилось, когда, какой ответ эталонный) доступен только
через HTTP API имитатора и файл JSONL; в OPC UA он не публикуется, иначе
станция могла бы «найти» причину в самом журнале.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence

from ..ownership import Owner, OwnershipRegistry
from .hoist import ASSUMPTIONS, BASELINE, KINDS, REQUIRED_SIGNALS, Cycle, GroundTruth, HoistState, build_cycle
from .recorded_modes import RecordedMode, load_recorded_modes
from .timeline import Change

logger = logging.getLogger("opcua-imitator.scenario")

Writer = Callable[[str, Any, datetime], Awaitable[bool]]
WriterFactory = Callable[[str], Writer]
Runner = Callable[["_Run"], Awaitable[None]]


class ScenarioError(Exception):
    """Отказ с машинным кодом для HTTP-ответа."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Run:
    """Один прогон: задача, свой статус, свой темп и своё владение."""

    def __init__(self, run_id: str, owner: Owner, signals: set, title: str) -> None:
        self.run_id = run_id
        self.owner = owner
        self.signals = set(signals)
        self.title = title
        self.task: Optional[asyncio.Task] = None
        # Писец прогона: writer_factory(run_id), ставится при запуске.
        self.writer: Optional[Writer] = None
        # Пауза: событие установлено — прогон идёт; снято — стоит (§9.1).
        self.pause_event = asyncio.Event()
        self.pre_pause_phase: str = "normal"
        self.pause_event.set()
        self.pause_started_mono: float = 0.0
        self.t0_wall: datetime = _utc_now()
        self.t0_mono: float = 0.0
        self.status: Dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "run_id": run_id,
            "seed": None,
            "kinds": None,
            "once": None,
            "episode": 0,
            "kind": None,
            "started_at": None,
            "writes": 0,
            "write_failures": 0,
            "max_lag_ms": 0,
            "error": None,
        }

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def card(self) -> Dict[str, Any]:
        """Краткая карточка прогона для списка в статусе."""
        return {
            "run_id": self.run_id,
            "title": self.title,
            "active": self.active,
            "phase": self.status["phase"],
            "kind": self.status["kind"],
            "episode": self.status["episode"],
            "owned_signals": sorted(self.signals),
        }


class ScenarioEngine:
    def __init__(
        self,
        writer_factory: WriterFactory,
        configs: Iterable[Mapping[str, Any]],
        *,
        simulated: bool,
        ownership: Optional[OwnershipRegistry] = None,
        journal_path: Optional[Path] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        journal_limit: int = 200,
        on_transition: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._writer_factory = writer_factory
        self._ownership = ownership
        # Граница жизненного цикла для снимка состояния (шаг 8, §13.2):
        # пауза, остановка, завершение и ошибка — момент сохранить.
        self._on_transition = on_transition
        self._configs: Dict[str, Mapping[str, Any]] = {cfg["id"]: cfg for cfg in configs}
        self._known = tuple(self._configs)
        # Воспроизводимые режимы из реальных записей. Оставляем только те,
        # что могут хоть что-то опубликовать на этой конфигурации сигналов.
        self._recorded: Dict[str, RecordedMode] = {
            mid: mode
            for mid, mode in load_recorded_modes(config=self._configs.values()).items()
            if mode.initial
        }
        self._journal_path = journal_path
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._journal: Deque[Dict[str, Any]] = deque(maxlen=journal_limit)
        # Прогоны по порядку запуска; последний — источник «общего» статуса.
        self._runs: Dict[str, _Run] = {}
        self._missing = sorted(REQUIRED_SIGNALS - set(self._known))
        if not simulated:
            self._unavailable: Optional[str] = "real_mode"
        elif self._missing:
            self._unavailable = "missing_signals"
        else:
            self._unavailable = None

    # -- чтение -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._unavailable is None

    @property
    def running(self) -> bool:
        return any(run.active for run in self._runs.values())

    def describe(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "unavailable_reason": self._unavailable,
            "missing_signals": list(self._missing),
            "kinds": [
                {"kind": item.kind, "title": item.title, "fault": item.fault} for item in KINDS.values()
            ],
            "modes": [
                {
                    "mode": mode.id,
                    "title": mode.title,
                    "fault": mode.fault,
                    "description": mode.description,
                    "expected_answer": mode.expected_answer,
                    "signals": sorted(mode.initial),
                    "changes": len(mode.changes),
                    "duration_ms": mode.duration_ms,
                    "source": mode.source,
                    "sha256": mode.sha256,
                }
                for mode in self._recorded.values()
            ],
            "assumptions": [
                {"signal_id": signal_id, "value": BASELINE[signal_id], "note": note}
                for signal_id, note in ASSUMPTIONS.items()
                if signal_id in self._configs
            ],
            "status": self.status(),
        }

    def status(self) -> Dict[str, Any]:
        """Общий статус: поля последнего запущенного прогона + список прогонов.

        Поля верхнего уровня сохраняют прежний формат (UI читает их как
        раньше); ``runs`` добавляет карточки всех прогонов сессии. Полный
        список владельцев сигналов — в реестре владения, наружу он идёт
        карточками ``owner`` в /api/signals.
        """
        runs = list(self._runs.values())
        active = [run for run in runs if run.active]
        latest = active[-1] if active else (runs[-1] if runs else None)
        if latest is not None:
            base = dict(latest.status)
        else:
            base = {
                "running": False,
                "phase": "idle",
                "run_id": None,
                "seed": None,
                "kinds": None,
                "once": None,
                "episode": 0,
                "kind": None,
                "started_at": None,
                "writes": 0,
                "write_failures": 0,
                "max_lag_ms": 0,
                "error": None,
            }
        base["running"] = self.running
        base["runs"] = [run.card() for run in runs]
        return base

    def journal(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Эпизоды, новые первыми."""
        return list(itertools.islice(reversed(self._journal), max(0, limit)))

    # -- управление ---------------------------------------------------------

    def _declared_signals(self, *, synthetic_kinds: Optional[List[str]], mode: Optional[RecordedMode]) -> List[str]:
        """Набор сигналов, которые прогон собирается менять (объявление до старта).

        Синтез пишет BASELINE целиком; запись режима — начальный кадр плюс
        все сигналы смен. Оба пересекаются с известными сигналами конфигурации.
        """
        if mode is not None:
            touched = set(mode.initial) | {change.signal_id for change in mode.changes}
        else:
            touched = set(BASELINE)
        return sorted(touched & set(self._known))

    def _launch(
        self,
        *,
        title: str,
        signals: Sequence[str],
        runner: Runner,
        name: str,
        **status: Any,
    ) -> Dict[str, Any]:
        """Заявить владение набором и поднять задачу прогона; конфликт — весь отказ."""
        run_id = uuid.uuid4().hex[:12]
        owner = Owner(id=f"scenario:{run_id}", kind="scenario", title=title)
        if self._ownership is not None:
            conflicts = self._ownership.acquire(owner, signals)
            if conflicts:
                listing = "; ".join(f"{c.signal_id} — «{c.owner.title}»" for c in conflicts)
                raise ScenarioError(
                    "signals_busy",
                    f"Запуск отклонён: сигналы под владением другого прогона ({listing})",
                )
        run = _Run(run_id=run_id, owner=owner, signals=set(signals), title=title)
        run.t0_wall = self._clock()
        run.t0_mono = self._monotonic()
        run.writer = self._writer_factory(run_id)
        run.status.update(phase="starting", started_at=_iso(run.t0_wall), **status)
        run.task = asyncio.create_task(self._supervise(run, runner), name=name)
        self._runs[run_id] = run
        logger.info(
            "scenario run %s started: title=%s signals=%d", run_id, title, len(signals)
        )
        return self.status()

    async def start(
        self,
        *,
        kinds: Optional[Sequence[str]] = None,
        seed: Optional[int] = None,
        once: bool = False,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Запустить прогон.

        ``mode=<id>`` запускает воспроизведение записи режима (взаимно
        исключает ``kinds``: синтезированные циклы). Без ``mode`` — обычный
        синтез циклов по ``kinds``. Несколько прогонов живут одновременно,
        если их наборы сигналов не пересекаются (§9.2): пересекающийся запуск
        отклоняется целиком, прежние прогоны не трогаются.
        """
        if mode is not None:
            return await self._start_recorded(mode, seed=seed)
        if not self.available:
            raise ScenarioError(
                self._unavailable or "unavailable",
                "Сценарии недоступны: "
                + (
                    "режим real пишет во внешний сервер"
                    if self._unavailable == "real_mode"
                    else "в конфигурации нет сигналов модели"
                ),
            )
        chosen = list(kinds) if kinds else list(KINDS)
        unknown = [kind for kind in chosen if kind not in KINDS]
        if unknown:
            raise ScenarioError("unknown_kind", f"Неизвестные сценарии: {', '.join(unknown)}")
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)

        async def runner(run: _Run) -> None:
            await self._run(run, seed=int(seed), kinds=chosen, once=once)

        return self._launch(
            title=f"Сценарий «{', '.join(chosen)}»",
            signals=self._declared_signals(synthetic_kinds=chosen, mode=None),
            runner=runner,
            name="opcua-scenario",
            seed=seed,
            kinds=chosen,
            once=once,
            episode=0,
            kind=None,
            writes=0,
            write_failures=0,
            max_lag_ms=0,
            error=None,
        )

    async def _start_recorded(self, mode_id: str, *, seed: Optional[int]) -> Dict[str, Any]:
        """Запустить воспроизведение записи режима одним проходом."""
        if self._unavailable == "real_mode":
            raise ScenarioError("real_mode", "Режим real пишет во внешний сервер")
        mode = self._recorded.get(mode_id)
        if mode is None:
            raise ScenarioError("unknown_mode", f"Неизвестный режим записи: {mode_id}")
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)

        async def runner(run: _Run) -> None:
            await self._run_recorded(run, mode, seed=int(seed))

        return self._launch(
            title=f"Режим «{mode.title}»",
            signals=self._declared_signals(synthetic_kinds=None, mode=mode),
            runner=runner,
            name="opcua-scenario-replay",
            seed=seed,
            kinds=None,
            once=True,
            episode=0,
            kind=mode_id,
            writes=0,
            write_failures=0,
            max_lag_ms=0,
            error=None,
        )

    def _select_run(self, run_id: Optional[str], *, active_only: bool) -> _Run:
        """Прогон по id или единственный активный; неоднозначность — ошибка."""
        if run_id is not None:
            run = self._runs.get(run_id)
            if run is None:
                raise ScenarioError("unknown_run", f"Неизвестный прогон: {run_id}")
            if active_only and not run.active:
                raise ScenarioError(
                    "not_running", f"Прогон {run_id} не активен (фаза {run.status['phase']})"
                )
            return run
        active = [run for run in self._runs.values() if run.active]
        if not active:
            raise ScenarioError("not_running", "Нет активного прогона")
        if len(active) > 1:
            names = ", ".join(f"{run.run_id} ({run.title})" for run in active)
            raise ScenarioError(
                "ambiguous_run", f"Активно несколько прогонов, укажите run_id: {names}"
            )
        return active[0]

    def _notify_transition(self, phase: str) -> None:
        """Сообщить наблюдателю о смене фазы (сбой наблюдателя не критичен)."""
        if self._on_transition is None:
            return
        try:
            self._on_transition(phase)
        except Exception:
            logger.exception("snapshot hook failed on phase %s", phase)

    async def pause(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Пауза прогона (§9.1): смена потоков замирает, владение сохраняется."""
        run = self._select_run(run_id, active_only=True)
        if run.pause_event.is_set():
            run.pause_started_mono = self._monotonic()
            run.pre_pause_phase = run.status["phase"]
            run.pause_event.clear()
            run.status["phase"] = "paused"
            self._notify_transition("paused")
            logger.info("scenario run %s paused", run.run_id)
        return self.status()

    async def resume(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Продолжить паузу: интервалы сценария остаются точными — база времени
        сдвигается на длительность паузы (§9.1)."""
        run = self._select_run(run_id, active_only=True)
        if not run.pause_event.is_set():
            delta = self._monotonic() - run.pause_started_mono
            run.t0_mono += delta
            run.t0_wall = run.t0_wall + timedelta(seconds=delta)
            run.pause_event.set()
            run.status["phase"] = run.pre_pause_phase
            logger.info("scenario run %s resumed after %.3fs pause", run.run_id, delta)
        return self.status()

    async def stop(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Остановить прогон(ы); значения сигналов остаются как были (§9.3).

        Остановка — явное решение оператора, поэтому освобождает владение, в
        том числе у прогона, вставшего ошибкой и ждавшего решения.
        """
        targets = [run for rid, run in self._runs.items() if run_id in (None, rid)]
        for run in targets:
            task = run.task
            if run.active:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                run.status["phase"] = "stopped"
                self._notify_transition("stopped")
                logger.info("scenario run %s stopped", run.run_id)
            elif run.status["phase"] == "error":
                run.status["phase"] = "stopped"
                self._notify_transition("stopped")
                logger.info("scenario run %s (error) acknowledged stopped", run.run_id)
            if run.status["phase"] in ("stopped", "finished") and self._ownership is not None:
                self._ownership.release(run.owner.id)
        return self.status()

    # -- проигрывание -------------------------------------------------------

    async def _supervise(self, run: _Run, runner: Runner) -> None:
        """Обёртка прогона: фаза завершения и освобождение владения в одном месте.

        Ошибка — прогон стоит с фазой ``error`` и держит владение (§9.2: условия
        продолжения не меняются, пока оператор не решит); завершение и отмена
        освобождают набор сразу.
        """
        try:
            await runner(run)
            run.status["phase"] = "finished"
            self._notify_transition("finished")
        except asyncio.CancelledError:
            run.status["phase"] = "stopped"
            self._notify_transition("stopped")
        except Exception as exc:  # цикл не должен падать молча
            logger.exception("scenario run %s crashed", run.run_id)
            run.status["phase"] = "error"
            run.status["error"] = type(exc).__name__
            self._notify_transition("error")
        finally:
            if run.status["phase"] in ("finished", "stopped") and self._ownership is not None:
                self._ownership.release(run.owner.id)

    async def _run(self, run: _Run, seed: int, kinds: List[str], once: bool) -> None:
        rng = random.Random(seed)
        plant = HoistState()
        for signal_id, value in BASELINE.items():
            if signal_id in self._configs:
                await self._write(run, signal_id, value, run.t0_wall)
        current: Dict[str, Any] = dict(BASELINE)
        offset_ms = 1000  # исправное состояние стоит секунду до первого рейса
        episode = 0
        while True:
            kind = rng.choice(kinds)
            cycle, current = build_cycle(kind, rng, plant, current, self._known)
            episode += 1
            run.status.update(episode=episode, kind=kind, phase="normal")
            await self._replay_changes(
                run, cycle.changes, cycle.truth, episode, offset_ms
            )
            offset_ms += cycle.duration_ms
            if once:
                break
        run.status["phase"] = "finished"

    def _mode_truth(self, mode: RecordedMode) -> GroundTruth:
        """Эталон записи режима: причина — первый момент, когда что-то зашевелилось."""
        return GroundTruth(
            kind=mode.id,
            title=mode.title,
            fault=mode.fault,
            causes=mode.causes,
            cause_at_ms=mode.cause_at_ms,
            stopped_at_ms=mode.stopped_at_ms,
            recovered_at_ms=mode.recovered_at_ms,
            expected_answer=mode.expected_answer,
            cause_visible_after_stop=mode.cause_visible_after_stop,
        )

    async def _run_recorded(self, run: _Run, mode: RecordedMode, seed: int) -> None:
        """Проиграть реальную запись режима: сначала кадр t=0, затем смены в темпе записи."""
        run.status.update(episode=1, phase="normal")
        for signal_id, value in mode.initial.items():
            if signal_id in self._configs:
                await self._write(run, signal_id, value, run.t0_wall)
        truth = self._mode_truth(mode)
        await self._replay_changes(run, mode.changes, truth, 1, 0)
        run.status["phase"] = "finished"

    async def _replay_changes(
        self,
        run: _Run,
        changes: Sequence[Change],
        truth: GroundTruth,
        episode: int,
        offset_ms: int,
    ) -> None:
        """Проиграть шкалу изменений в реальном времени (общее ядро для циклов и режимов).

        ``changes`` упорядочены по моменту; таймстампы в OPC UA следуют за
        моментом сценария, а не за моментом записи. Фазы и журнал правды
        определяются маркерами ``truth`` (причина/остановка/возврат). Пауза
        (§9.1) проверяется между группами смен; база времени сдвигается на
        длительность паузы в resume, поэтому интервалы остаются точными.
        """
        recorded = False
        for at_ms, group in itertools.groupby(changes, key=lambda change: change.at_ms):
            await run.pause_event.wait()
            absolute_ms = offset_ms + at_ms
            delay = run.t0_mono + absolute_ms / 1000.0 - self._monotonic()
            if delay > 0:
                await self._sleep(delay)
            else:
                lag = int(-delay * 1000)
                if lag > run.status["max_lag_ms"]:
                    run.status["max_lag_ms"] = lag
            if truth.recovered_at_ms is not None and at_ms >= truth.recovered_at_ms:
                run.status["phase"] = "normal"
            elif at_ms >= truth.stopped_at_ms:
                run.status["phase"] = "stopped"
            elif at_ms >= truth.cause_at_ms:
                run.status["phase"] = "episode"
            if not recorded and at_ms >= truth.cause_at_ms:
                self._record(run, episode, truth, offset_ms)
                recorded = True
            when = run.t0_wall + timedelta(milliseconds=absolute_ms)
            for change in group:
                await self._write(run, change.signal_id, change.value, when)

    async def _write(self, run: _Run, signal_id: str, value: Any, when: datetime) -> None:
        ok = await run.writer(signal_id, value, when)
        run.status["writes"] += 1
        if not ok:
            run.status["write_failures"] += 1

    def _record(
        self,
        run: _Run,
        episode: int,
        truth: GroundTruth,
        offset_ms: int,
    ) -> None:
        t0_wall = run.t0_wall

        def at(ms: int) -> str:
            return _iso(t0_wall + timedelta(milliseconds=offset_ms + ms))

        record = {
            "run_id": run.run_id,
            "seed": run.status["seed"],
            "episode": episode,
            "kind": truth.kind,
            "title": truth.title,
            "fault": truth.fault,
            "causes": [
                {
                    "signal_id": cause.signal_id,
                    "name": self._configs.get(cause.signal_id, {}).get("name"),
                    "project_tag": self._configs.get(cause.signal_id, {}).get("project_tag"),
                    "before": cause.before,
                    "after": cause.after,
                }
                for cause in truth.causes
            ],
            "cause_at": at(truth.cause_at_ms),
            "stopped_at": at(truth.stopped_at_ms),
            "recovered_at": at(truth.recovered_at_ms),
            "cause_visible_after_stop": truth.cause_visible_after_stop,
            "expected_answer": truth.expected_answer,
        }
        self._journal.append(record)
        if self._journal_path is not None:
            try:
                self._journal_path.parent.mkdir(parents=True, exist_ok=True)
                with self._journal_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                logger.warning("scenario journal is not writable", exc_info=True)
