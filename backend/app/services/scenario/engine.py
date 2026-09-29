"""Движок сценариев: проигрывает циклы установки в реальном времени.

Один движок на приложение, одна фоновая задача. Сначала пишется исправное
состояние, затем цикл за циклом: штатные рейсы, эпизод (авария или штатная
остановка), возврат в работу. Каждое изменение уходит в адресное
пространство с SourceTimestamp момента сценария — потребитель видит точные
интервалы между событиями, даже если запись чуть запоздала.

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

from .hoist import ASSUMPTIONS, BASELINE, KINDS, REQUIRED_SIGNALS, Cycle, GroundTruth, HoistState, build_cycle
from .recorded_modes import RecordedMode, load_recorded_modes
from .timeline import Change

logger = logging.getLogger("opcua-imitator.scenario")

Writer = Callable[[str, Any, datetime], Awaitable[bool]]


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


class ScenarioEngine:
    def __init__(
        self,
        writer: Writer,
        configs: Iterable[Mapping[str, Any]],
        *,
        simulated: bool,
        journal_path: Optional[Path] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        journal_limit: int = 200,
    ) -> None:
        self._writer = writer
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
        self._task: Optional[asyncio.Task] = None
        self._missing = sorted(REQUIRED_SIGNALS - set(self._known))
        if not simulated:
            self._unavailable: Optional[str] = "real_mode"
        elif self._missing:
            self._unavailable = "missing_signals"
        else:
            self._unavailable = None
        self._status: Dict[str, Any] = {
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

    # -- чтение -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._unavailable is None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

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
        status = dict(self._status)
        status["running"] = self.running
        return status

    def journal(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Эпизоды, новые первыми."""
        return list(itertools.islice(reversed(self._journal), max(0, limit)))

    # -- управление ---------------------------------------------------------

    async def start(
        self,
        *,
        kinds: Optional[Sequence[str]] = None,
        seed: Optional[int] = None,
        once: bool = False,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Запустить проигрывание.

        ``mode=<id>`` запускает воспроизведение записи режима (взаимно
        исключает ``kinds``: синтезированные циклы). Без ``mode`` — обычный
        синтез циклов по ``kinds``.
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
        await self.stop()
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)
        run_id = uuid.uuid4().hex[:12]
        self._status.update(
            phase="starting",
            run_id=run_id,
            seed=seed,
            kinds=chosen,
            once=once,
            episode=0,
            kind=None,
            started_at=_iso(self._clock()),
            writes=0,
            write_failures=0,
            max_lag_ms=0,
            error=None,
        )
        self._task = asyncio.create_task(self._run(run_id, seed, chosen, once), name="opcua-scenario")
        logger.info("scenario run %s started: kinds=%s seed=%s once=%s", run_id, chosen, seed, once)
        return self.status()

    async def stop(self) -> Dict[str, Any]:
        """Остановить проигрывание; значения сигналов остаются как были."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._status["phase"] = "stopped"
            logger.info("scenario run %s stopped", self._status.get("run_id"))
        return self.status()

    async def _start_recorded(self, mode_id: str, *, seed: Optional[int]) -> Dict[str, Any]:
        """Запустить воспроизведение записи режима одним проходом."""
        if self._unavailable == "real_mode":
            raise ScenarioError("real_mode", "Режим real пишет во внешний сервер")
        mode = self._recorded.get(mode_id)
        if mode is None:
            raise ScenarioError("unknown_mode", f"Неизвестный режим записи: {mode_id}")
        await self.stop()
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)
        run_id = uuid.uuid4().hex[:12]
        self._status.update(
            phase="starting",
            run_id=run_id,
            seed=seed,
            kinds=None,
            once=True,
            episode=0,
            kind=mode_id,
            started_at=_iso(self._clock()),
            writes=0,
            write_failures=0,
            max_lag_ms=0,
            error=None,
        )
        self._task = asyncio.create_task(
            self._run_recorded(run_id, mode, seed), name="opcua-scenario-replay"
        )
        logger.info("scenario replay %s started: mode=%s", run_id, mode_id)
        return self.status()

    # -- проигрывание -------------------------------------------------------

    async def _run(self, run_id: str, seed: int, kinds: List[str], once: bool) -> None:
        try:
            rng = random.Random(seed)
            plant = HoistState()
            t0_wall = self._clock()
            t0_mono = self._monotonic()
            for signal_id, value in BASELINE.items():
                if signal_id in self._configs:
                    await self._write(signal_id, value, t0_wall)
            current: Dict[str, Any] = dict(BASELINE)
            offset_ms = 1000  # исправное состояние стоит секунду до первого рейса
            episode = 0
            while True:
                kind = rng.choice(kinds)
                cycle, current = build_cycle(kind, rng, plant, current, self._known)
                episode += 1
                self._status.update(episode=episode, kind=kind, phase="normal")
                await self._play(cycle, run_id, seed, episode, t0_wall, t0_mono, offset_ms)
                offset_ms += cycle.duration_ms
                if once:
                    break
            self._status["phase"] = "finished"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # цикл не должен падать молча
            logger.exception("scenario run %s crashed", run_id)
            self._status.update(phase="error", error=type(exc).__name__)

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

    async def _run_recorded(self, run_id: str, mode: RecordedMode, seed: int) -> None:
        """Проиграть реальную запись режима: сначала кадр t=0, затем смены в темпе записи."""
        try:
            t0_wall = self._clock()
            t0_mono = self._monotonic()
            self._status.update(episode=1, phase="normal")
            for signal_id, value in mode.initial.items():
                if signal_id in self._configs:
                    await self._write(signal_id, value, t0_wall)
            truth = self._mode_truth(mode)
            await self._replay_changes(
                mode.changes, truth, run_id, seed, 1, t0_wall, t0_mono, 0
            )
            self._status["phase"] = "finished"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("scenario replay %s crashed", run_id)
            self._status.update(phase="error", error=type(exc).__name__)

    async def _play(
        self,
        cycle: Cycle,
        run_id: str,
        seed: int,
        episode: int,
        t0_wall: datetime,
        t0_mono: float,
        offset_ms: int,
    ) -> None:
        await self._replay_changes(
            cycle.changes, cycle.truth, run_id, seed, episode, t0_wall, t0_mono, offset_ms
        )

    async def _replay_changes(
        self,
        changes: Sequence[Change],
        truth: GroundTruth,
        run_id: str,
        seed: int,
        episode: int,
        t0_wall: datetime,
        t0_mono: float,
        offset_ms: int,
    ) -> None:
        """Проиграть шкалу изменений в реальном времени (общее ядро для циклов и режимов).

        ``changes`` упорядочены по моменту; таймстампы в OPC UA следуют за
        моментом сценария, а не за моментом записи. Фазы и журнал правды
        определяются маркерами ``truth`` (причина/остановка/возврат).
        """
        recorded = False
        for at_ms, group in itertools.groupby(changes, key=lambda change: change.at_ms):
            absolute_ms = offset_ms + at_ms
            delay = t0_mono + absolute_ms / 1000.0 - self._monotonic()
            if delay > 0:
                await self._sleep(delay)
            else:
                lag = int(-delay * 1000)
                if lag > self._status["max_lag_ms"]:
                    self._status["max_lag_ms"] = lag
            if truth.recovered_at_ms is not None and at_ms >= truth.recovered_at_ms:
                self._status["phase"] = "normal"
            elif at_ms >= truth.stopped_at_ms:
                self._status["phase"] = "stopped"
            elif at_ms >= truth.cause_at_ms:
                self._status["phase"] = "episode"
            if not recorded and at_ms >= truth.cause_at_ms:
                self._record(run_id, seed, episode, truth, t0_wall, offset_ms)
                recorded = True
            when = t0_wall + timedelta(milliseconds=absolute_ms)
            for change in group:
                await self._write(change.signal_id, change.value, when)

    async def _write(self, signal_id: str, value: Any, when: datetime) -> None:
        ok = await self._writer(signal_id, value, when)
        self._status["writes"] += 1
        if not ok:
            self._status["write_failures"] += 1

    def _record(
        self,
        run_id: str,
        seed: int,
        episode: int,
        truth: GroundTruth,
        t0_wall: datetime,
        offset_ms: int,
    ) -> None:
        def at(ms: int) -> str:
            return _iso(t0_wall + timedelta(milliseconds=offset_ms + ms))

        record = {
            "run_id": run_id,
            "seed": seed,
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
