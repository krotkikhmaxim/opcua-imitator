"""SQLite-архив станции как источник плейбэка (read-only).

Архив станции ведёт ``src/telemetry_ingestion/archive_store.py`` — его DDL
здесь эталон, но не импорт: имитатор читает файл, не пакет станции.
Открытие только по URI ``file:…?mode=ro`` — плейбэк не может написать ни
байта в архив, а соединение без клиента станции живёт в stdlib sqlite3.

Отсчёты одного сигнала лежат в нескольких сериях (по одной на партию
записи), поэтому окно выбирается по ``series.signal_id`` и ``observed_us``
через индекс ``readings_window (series_id, observed_us)``. Поток отдаёт
изменения как ``(at_ms, {signal_id: value})`` — тот же контракт, что у
XlsxSource: ``at_ms`` от начала окна, ``t0`` — исходная метка окна, так что
SourceTimestamp в шине восстанавливает интервалы записи (честные метки на
скрабе, ради которых архивный плейбэк и существует). Качество транслируется
в good|uncertain|bad; ``bad`` в шину не пишется.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .. import signal_types

#: Написание типа колонки станции → имя в единой таблице.
_COLUMN_TYPES = {
    "boolean": "Boolean",
    "integer": "Int32",
    "int": "Int32",
    "long": "Int32",
    "float": "Double",
    "double": "Double",
}

#: Качество архива → корзина плейбэка; неизвестное не играется как good.
QUALITY_BUCKETS = {
    "good": "good",
    "uncertain": "uncertain",
    "bad": "bad",
}
#: Отсчёты с таким качеством в шину не идут (считаются в skipped_quality).
SKIPPED_QUALITIES = frozenset({"bad"})


class ArchiveError(Exception):
    """Отказ открытия окна архива с машинным кодом для HTTP-ответа."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArchiveSeriesInfo:
    """Серия отсчётов одного сигнала в списке ``/api/archive/series``."""

    signal_id: str
    value_type: str
    rows: int
    observed_min_us: int
    observed_max_us: int


def _connect(path: str) -> sqlite3.Connection:
    """Read-only соединение: без mode=ro соединение было бы молча читаемым.

    ``check_same_thread=False``: соединение рождается в рабочем потоке
    (открытие окна идёт через to_thread), а играет уже в цикле событий;
    после возврата из загрузки источник трогает только один поток.
    """
    if not os.path.isfile(path):
        raise ArchiveError("not_found", f"Архив не найден: {path}")
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise ArchiveError("unreadable", f"Архив нечитаем: {exc}") from exc


def list_series(path: str) -> Tuple[List[ArchiveSeriesInfo], Dict[str, Any]]:
    """Сводка по сигналу (серии одного сигнала слиты) и диапазон архива."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT s.signal_id, s.value_type, count(*),
                   min(r.observed_us), max(r.observed_us)
            FROM series s JOIN readings r ON r.series_id = s.series_id
            GROUP BY s.signal_id, s.value_type
            ORDER BY s.signal_id
            """
        ).fetchall()
        span = conn.execute(
            "SELECT min(observed_us), max(observed_us) FROM readings"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ArchiveError("unreadable", f"Архив нечитаем: {exc}") from exc
    finally:
        conn.close()
    series = [
        ArchiveSeriesInfo(
            signal_id=str(r[0]),
            value_type=str(r[1]),
            rows=int(r[2]),
            observed_min_us=int(r[3]),
            observed_max_us=int(r[4]),
        )
        for r in rows
    ]
    meta = {
        "observed_min_us": int(span[0]) if span and span[0] is not None else None,
        "observed_max_us": int(span[1]) if span and span[1] is not None else None,
    }
    return series, meta


def _coerce_value(value_type: str, value: Any) -> Any:
    """Значение колонки к типу серии; None для пустой и неприводимой ячейки.

    Написания типов колонок станции (boolean/integer/long/double) маппятся на
    единую таблицу signal_types (ТЗ #5 §11) — приведение одно на приложение.
    """
    if value is None:
        return None
    mapped = _COLUMN_TYPES.get(value_type.lower())
    if mapped is None:
        return str(value)
    try:
        return signal_types.coerce(mapped, value, contract="file")
    except (signal_types.SignalValueError, signal_types.SignalConfigError):
        return None


class ArchiveSource:
    """Открытое окно архива: поток изменений с исходными метками времени.

    Владеет read-only соединением; ``rows()`` можно звать повторно — курсор
    строится заново (как пересмотр листа в XlsxSource).
    """

    def __init__(
        self,
        path: str,
        *,
        signal_ids: Sequence[str],
        t0_us: int,
        start_us: int,
        end_us: int,
        rows_total: int,
        skipped_quality: int,
    ) -> None:
        self.path = path
        self.signal_ids = list(signal_ids)
        self.t0_us = t0_us
        self.start_us = start_us
        self.end_us = end_us
        self.rows_total = rows_total
        self.skipped_quality = skipped_quality
        self._conn = _connect(path)
        self.t0: Optional[datetime] = datetime.fromtimestamp(t0_us / 1_000_000, tz=timezone.utc)

    @property
    def name(self) -> str:
        return f"{os.path.basename(self.path)} (архив)"

    def rows(self) -> Iterator[Tuple[int, Dict[str, Any]]]:
        """Отсчёты окна по времени: ``(at_ms, {signal_id: value})``."""
        placeholders = ",".join("?" for _ in self.signal_ids)
        cursor = self._conn.execute(
            f"""
            SELECT r.observed_us, s.signal_id, s.value_type, r.value, r.quality
            FROM series s JOIN readings r ON r.series_id = s.series_id
            WHERE s.signal_id IN ({placeholders})
              AND r.observed_us >= ? AND r.observed_us <= ?
            ORDER BY r.observed_us
            """,
            (*self.signal_ids, self.start_us, self.end_us),
        )
        for observed_us, signal_id, value_type, value, quality in cursor:
            bucket = QUALITY_BUCKETS.get(str(quality), "uncertain")
            if bucket in SKIPPED_QUALITIES:
                continue
            coerced = _coerce_value(str(value_type), value)
            if coerced is None:
                continue
            yield int(observed_us - self.t0_us) // 1000, {str(signal_id): coerced}

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def open_archive(
    path: str,
    *,
    signal_ids: Optional[Sequence[str]] = None,
    start_us: Optional[int] = None,
    end_us: Optional[int] = None,
    max_changes: int,
) -> ArchiveSource:
    """Открыть окно архива; объём сверх лимита — отказ, а не усечение.

    ``signal_ids`` — подмножество сигналов (пустое/None — все); окно по
    ``observed_us`` включительно с обеих сторон. Отказы: не найден, окно
    пустое, отсчётов больше ``max_changes`` (PLAYBACK_MAX_CHANGES).
    """
    series, meta = list_series(path)
    if not series:
        raise ArchiveError("no_series", "В архиве нет отсчётов")
    available = {info.signal_id for info in series}
    wanted = [s for s in (signal_ids or []) if s] or sorted(available)
    missing = [s for s in wanted if s not in available]
    if missing:
        raise ArchiveError(
            "unknown_signals",
            f"Сигналов нет в архиве: {', '.join(missing[:5])}"
            + (f" и ещё {len(missing) - 5}" if len(missing) > 5 else ""),
        )
    lo = start_us if start_us is not None else (meta["observed_min_us"] or 0)
    hi = end_us if end_us is not None else (meta["observed_max_us"] or 0)
    if lo > hi:
        raise ArchiveError("bad_window", f"Начало окна ({lo}) позже конца ({hi})")

    conn = _connect(path)
    try:
        placeholders = ",".join("?" for _ in wanted)
        count, first, skipped = conn.execute(
            f"""
            SELECT count(*), min(r.observed_us),
                   sum(r.quality NOT IN ('good', 'uncertain'))
            FROM series s JOIN readings r ON r.series_id = s.series_id
            WHERE s.signal_id IN ({placeholders})
              AND r.observed_us >= ? AND r.observed_us <= ?
            """,
            (*wanted, lo, hi),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ArchiveError("unreadable", f"Архив нечитаем: {exc}") from exc
    finally:
        conn.close()
    rows_total = int(count or 0)
    if rows_total == 0 or first is None:
        raise ArchiveError(
            "empty_window", f"В окне {lo}..{hi} нет отсчётов выбранных сигналов"
        )
    if rows_total > max_changes:
        raise ArchiveError(
            "too_many_changes",
            f"Окно содержит {rows_total} отсчётов — больше лимита плейбэка "
            f"{max_changes} (PLAYBACK_MAX_CHANGES); сузьте окно или набор сигналов",
        )
    return ArchiveSource(
        path,
        signal_ids=wanted,
        t0_us=int(first),
        start_us=lo,
        end_us=hi,
        rows_total=rows_total,
        skipped_quality=int(skipped or 0),
    )
