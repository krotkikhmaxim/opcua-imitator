"""Плейбэк тренда: поток строк XLSX в шину в темпе записи.

Тот же контракт времени, что у сценарного движка: логическая позиция —
перебазированный офсет плюс ход часов × скорость; пауза, возобновление и
смена скорости перебазируют шкалу, а не правят прошлое. Значения уходят в
шину с SourceTimestamp момента тренда (метка строки файла) — потребитель
видит интервалы записи, а не момент воспроизведения.

Seek не гарантирует монотонность меток: файл пересматривается до цели,
значения на цель пишутся текущим wall-временем, дальше метки снова
следуют за трендом (честные метки на скрабе даёт архивный плейбэк,
фаза 2). Просмотр вглубь длинного файла — скан с начала; шаги по 2000
строк отдают управление циклу событий, чтобы API оставался живым.

Источник отдаёт только изменения (первый кадр — весь); пропуски и nan не
создают записей. Сервис не знает ни FastAPI, ни OPC UA: writer и часы
приходят параметрами, поэтому транспорт тестируется на виртуальном
времени. Взаимное исключение с другими драйверами обеспечивает реестр —
сервис только помечает, держит ли он шину.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from .archive_reader import ArchiveError, open_archive
from .xlsx import PlaybackSourceError, XlsxSource, open_source

logger = logging.getLogger("opcua-imitator.playback")

Writer = Callable[[str, Any, datetime], Awaitable[bool]]
Clock = Callable[[], datetime]

#: шаг цикла обслуживания (пауза, ожидание pacing) в секундах
_TICK_S = 0.05
#: сколько строк скана между уступками циклу событий
_SCAN_YIELD_ROWS = 2000

MIN_RATE = 0.25
MAX_RATE = 8.0


class PlaybackError(Exception):
    """Отказ управления плейбэком с машинным кодом для HTTP-ответа."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PlaybackService:
    """Один плейбэк на приложение: load → play/pause/seek/rate → stop."""

    def __init__(
        self,
        writer: Writer,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Clock = _utc_now,
        writes_enabled: bool = True,
    ) -> None:
        self._writer = writer
        self._monotonic = monotonic
        self._sleep = sleep
        self._clock = clock
        # Заморозка таймлайна (ТЗ #5, §2): False — play/seek отклоняются,
        # ни одна строка тренда не доходит до writer. Флаг ставится при
        # создании (из настроек) и не меняется на ходу.
        self._writes_enabled = writes_enabled
        self._source: Optional[XlsxSource] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._holds = False
        self._paused = False
        self._rate = 1.0
        self._offset_ms = 0.0
        self._t0_mono = 0.0
        self._duration_ms: Optional[int] = None
        self._start_ms = 0
        self._end_ms: Optional[int] = None
        self._status: Dict[str, Any] = self._fresh_status()

    @property
    def writes_enabled(self) -> bool:
        """Запись из таймлайна в сигналы разрешена (не заморожен)."""
        return self._writes_enabled

    def _fresh_status(self) -> Dict[str, Any]:
        return {
            "loaded": False,
            "source": None,
            "t0": None,
            "start_ms": 0,
            "end_ms": None,
            "phase": "idle",
            "mapped_signals": 0,
            "unmatched_columns": 0,
            "ambiguous": 0,
            "position_ms": 0,
            "duration_ms": None,
            "rate": 1.0,
            "paused": False,
            "writes_enabled": self._writes_enabled,
            "rows_played": 0,
            "writes": 0,
            "write_failures": 0,
            "max_lag_ms": 0,
            "error": None,
        }

    # -- загрузка -----------------------------------------------------------

    def load(
        self,
        path: str,
        configs: Mapping[str, Mapping[str, Any]],
        *,
        max_bytes: int,
    ) -> Dict[str, Any]:
        """Открыть файл и сопоставить колонки; запущенный прогон останавливается."""
        try:
            source = open_source(path, configs, max_bytes=max_bytes)
        except PlaybackSourceError as exc:
            raise PlaybackError(exc.code, exc.message)
        return self.load_from(
            source,
            mapped_signals=len(source.columns),
            unmatched_columns=len(source.unmatched),
            ambiguous=len(source.ambiguous),
        )

    def load_from(
        self,
        source: Any,
        *,
        mapped_signals: int,
        unmatched_columns: int = 0,
        ambiguous: int = 0,
    ) -> Dict[str, Any]:
        """Взять готовый источник (XLSX или архив); общий сброс состояния.

        Неудачная перезагрузка (исключение до этой строки) оставляет прежний
        источник загруженным.
        """
        self._require_stopped_task()
        if self._source is not None:
            self._source.close()
        self._source = source
        self._holds = False
        self._paused = False
        self._rate = 1.0
        self._offset_ms = 0.0
        self._duration_ms = None
        self._status = self._fresh_status()
        self._status.update(
            loaded=True,
            source=source.name,
            t0=getattr(source, "t0", None),
            mapped_signals=mapped_signals,
            unmatched_columns=unmatched_columns,
            ambiguous=ambiguous,
        )
        return self.status()

    def unload(self) -> Dict[str, Any]:
        self._require_stopped_task()
        if self._source is not None:
            self._source.close()
            self._source = None
        self._holds = False
        self._status = self._fresh_status()
        return self.status()

    def load_archive(
        self,
        archive_path: str,
        *,
        signal_ids: Optional[List[str]] = None,
        start_us: Optional[int] = None,
        end_us: Optional[int] = None,
        max_changes: int,
    ) -> Dict[str, Any]:
        """Загрузить окно SQLite-архива станции; метки времени — исходные.

        Отсчёты на каждый такт записи (не только изменения) — дедупликацию
        делает проход, как для XLSX. ``bad``-качество в шину не идёт и
        считается в статусе источника.
        """
        try:
            source = open_archive(
                archive_path,
                signal_ids=signal_ids,
                start_us=start_us,
                end_us=end_us,
                max_changes=max_changes,
            )
        except ArchiveError as exc:
            raise PlaybackError(exc.code, exc.message)
        return self.load_from(source, mapped_signals=len(source.signal_ids))

    @property
    def loaded(self) -> bool:
        return self._source is not None

    @property
    def bus_held(self) -> bool:
        """Плейбэк держит шину от запуска до остановки/финала/выгрузки."""
        return self._holds

    # -- управление ---------------------------------------------------------

    def play(
        self, start_ms: int = 0, end_ms: Optional[int] = None
    ) -> Dict[str, Any]:
        """Пустить прогон; ``start_ms``/``end_ms`` задают участок тренда.

        Начало не из нуля доматывается сканом без записей (семантика seek),
        значения на старте участка пишутся текущим wall-временем. Конец
        включителен: строка ``end_ms`` пишется, дальше прогон завершается.
        """
        if not self._writes_enabled:
            raise PlaybackError(
                "frozen",
                "Таймлайн отключён от управления сервером (заморожен)",
            )
        if not self.loaded:
            raise PlaybackError("not_loaded", "Сначала загрузите файл тренда")
        if self._task is not None and not self._task.done():
            raise PlaybackError("already_running", "Плейбэк уже идёт")
        if start_ms < 0:
            raise PlaybackError(
                "bad_position", "Начало участка не может быть отрицательным"
            )
        if end_ms is not None and end_ms <= start_ms:
            raise PlaybackError("bad_window", "Конец участка должен быть позже начала")
        self._holds = True
        self._paused = False
        self._rate = 1.0
        self._offset_ms = float(start_ms)
        self._t0_mono = self._monotonic()  # до первого status(): иначе позиция от нулевой базы
        self._start_ms = int(start_ms)
        self._end_ms = end_ms
        self._duration_ms = None
        self._status.update(
            phase="playing",
            paused=False,
            rate=1.0,
            start_ms=self._start_ms,
            end_ms=self._end_ms,
            rows_played=0,
            writes=0,
            write_failures=0,
            max_lag_ms=0,
            error=None,
        )
        self._task = asyncio.create_task(self._run())
        return self.status()

    def pause(self) -> Dict[str, Any]:
        self._require_running("пауза")
        if self._paused:
            return self.status()
        self._offset_ms = self.position_ms()
        self._paused = True
        self._status.update(paused=True, phase="paused", position_ms=int(self._offset_ms))
        return self.status()

    def resume(self) -> Dict[str, Any]:
        self._require_running("возобновление")
        if not self._paused:
            return self.status()
        self._t0_mono = self._monotonic()
        self._paused = False
        self._status.update(paused=False, phase="playing")
        return self.status()

    def set_rate(self, rate: float) -> Dict[str, Any]:
        self._require_running("смена скорости")
        if not MIN_RATE <= rate <= MAX_RATE:
            raise PlaybackError(
                "bad_rate", f"Скорость плейбэка — от {MIN_RATE} до {MAX_RATE}"
            )
        self._offset_ms = self.position_ms()
        self._t0_mono = self._monotonic()
        self._rate = rate
        self._status["rate"] = rate
        return self.status()

    async def seek(self, target_ms: int) -> Dict[str, Any]:
        """Перейти на позицию: значения на цель — текущим временем, дальше по тренду.

        Позиция за уже проигранным диапазоном допустима — скан сам обнаружит
        конец файла и остановится на последней строке.
        """
        if not self._writes_enabled:
            raise PlaybackError(
                "frozen",
                "Таймлайн отключён от управления сервером (заморожен)",
            )
        self._require_running("переход")
        if target_ms < 0:
            raise PlaybackError("bad_position", "Позиция не может быть отрицательной")
        was_paused = self._paused
        # текущий проход завершится, увидев событие (проверка на каждой строке
        # и в каждом такте ожидания — максимум ~0.25 с)
        self._stop_event.set()
        task = self._task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._offset_ms = float(target_ms)
        self._t0_mono = self._monotonic()
        self._paused = was_paused
        self._status.update(
            position_ms=target_ms,
            paused=was_paused,
            phase="paused" if was_paused else "playing",
        )
        self._task = asyncio.create_task(self._run())
        return self.status()

    def stop(self) -> Dict[str, Any]:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._holds = False
        self._paused = False
        position = int(self._status.get("position_ms", 0))
        self._status.update(phase="stopped", paused=False, position_ms=position)
        return self.status()

    # -- статус и позиция ---------------------------------------------------

    def position_ms(self) -> float:
        """Логическая позиция: офсет базы плюс ход часов × скорость."""
        if self._paused or self._task is None or self._task.done():
            return self._offset_ms
        return self._offset_ms + (self._monotonic() - self._t0_mono) * 1000.0 * self._rate

    def status(self) -> Dict[str, Any]:
        self._status["position_ms"] = int(max(0.0, self.position_ms()))
        self._status["duration_ms"] = self._duration_ms
        # флаг заморозки — текущий, а не от последнего load/unload
        self._status["writes_enabled"] = self._writes_enabled
        if self._source is not None:
            self._status["loaded"] = True
        return dict(self._status)

    def journal_notes(self) -> Dict[str, Any]:
        """Сводка для сверки со станцией: файл, диапазон, число записей."""
        return {
            "source": self._status["source"],
            "position_ms": self._status["position_ms"],
            "duration_ms": self._status["duration_ms"],
            "writes": self._status["writes"],
        }

    # -- внутреннее ---------------------------------------------------------

    def _require_running(self, action: str) -> None:
        if self._task is None or self._task.done():
            raise PlaybackError(
                "not_running", f"«{action}» доступно только у идущего плейбэка"
            )

    def _require_stopped_task(self) -> None:
        if self._task is not None and not self._task.done():
            raise PlaybackError("running", "Остановите плейбэк перед этой операцией")

    async def _run(self) -> None:
        assert self._source is not None
        source = self._source
        self._stop_event = asyncio.Event()
        base = self._offset_ms
        self._t0_mono = self._monotonic()
        scanning = base > 0  # после seek: домотать до базы без записей
        try:
            last: Dict[str, Any] = {}
            pending: List[tuple] = []
            rows_played = 0
            last_at_ms = base
            for at_ms, values in source.rows():
                if self._stop_event.is_set():
                    return
                if scanning:
                    if at_ms <= base:
                        last.update(values)
                        rows_played += 1
                        self._status["rows_played"] = rows_played
                        if rows_played % _SCAN_YIELD_ROWS == 0:
                            await asyncio.sleep(0)
                        continue
                    await self._flush_target(last)
                    scanning = False
                moment = (source.t0 or self._clock()) + timedelta(milliseconds=at_ms)
                for signal_id, value in values.items():
                    if signal_id not in last or last[signal_id] != value:
                        pending.append((moment, signal_id, value))
                        last[signal_id] = value
                # темп держит каждая строка, а не только изменившаяся:
                # тихий участок должен идти реальным временем, иначе
                # прогон доскакивает до конца окна мгновенно
                await self._pace(at_ms)
                if self._stop_event.is_set():
                    return
                for moment, signal_id, value in pending:
                    ok = await self._writer(signal_id, value, moment)
                    self._status["writes"] += 1
                    if not ok:
                        self._status["write_failures"] += 1
                pending.clear()
                rows_played += 1
                self._status["rows_played"] = rows_played
                self._status["position_ms"] = at_ms
                self._duration_ms = at_ms
                last_at_ms = at_ms
                if self._end_ms is not None and at_ms >= self._end_ms:
                    break  # конец участка включителен — строка end_ms написана
            if scanning:
                # база за концом файла: значения на последней строке — на цель
                await self._flush_target(last)
            # финал: позиция — последняя сыгранная строка, а не база старта
            self._offset_ms = float(last_at_ms)
            self._status["position_ms"] = int(last_at_ms)
            self._status["phase"] = "finished"
            self._holds = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("playback crashed")
            self._status.update(phase="error", error=type(exc).__name__)
            self._holds = False

    async def _flush_target(self, last: Dict[str, Any]) -> None:
        """Значения на точке перехода — текущим wall-временем (семантика seek)."""
        now = self._clock()
        for signal_id, value in last.items():
            ok = await self._writer(signal_id, value, now)
            self._status["writes"] += 1
            if not ok:
                self._status["write_failures"] += 1

    async def _pace(self, at_ms: float) -> None:
        """Дождаться логического момента ``at_ms``; пауза держит позицию."""
        while True:
            if self._stop_event.is_set():
                return
            if self._paused:
                await self._sleep(_TICK_S)
                continue
            delay = (
                self._t0_mono + (at_ms - self._offset_ms) / (1000.0 * self._rate)
                - self._monotonic()
            )
            if delay <= 0:
                lag = int(-delay * 1000)
                if lag > self._status["max_lag_ms"]:
                    self._status["max_lag_ms"] = lag
                return
            await self._sleep(min(delay, 0.25))
