"""Открытие XLSX-тренда как потока строк для плейбэка.

Формат — экспорт трендов ПЛК: шапка из строк-заголовков (маркеры групп
``[0:N]``, пути ``Application.*``, единицы), затем строки данных с меткой
времени в первой колонке (``DD.MM.YYYY HH:MM:SS.ffffff``). Поддерживаются
и записи режимов (2 строки заголовков), и экспорт владельца (3 строки,
сотни мегабайт): строки читаются потоково, ``read_only=True``.

Колонки сопоставляются опубликованным сигналам по двум последним сегментам
пути после нормализации: проект ПЛК вкладывает контейнеры иначе, чем
экспорт биндингов (``GVL`` появляется и исчезает, ``Drive_GVL``/``DriveGVL``),
и точное совпадение путей не выдерживает даже между соседними экспортами.
Сопоставление честное: колонка с неоднозначным ключом (одинаковый путь у
двух каналов) и непубликуемые колонки остаются в ``unmatched``/``ambiguous`` —
плейбэк не угадывает. Ячейки-пропуски (``nan``, пустые) не создают значений.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import openpyxl

from .. import signal_types

_TS_FORMAT = "%d.%m.%Y %H:%M:%S.%f"


class PlaybackSourceError(Exception):
    """Отказ открытия источника с машинным кодом для HTTP-ответа."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MappedColumn:
    """Колонка тренда, сопоставленная опубликованному сигналу."""

    index: int
    signal_id: str
    signal_type: str


@dataclass
class XlsxSource:
    """Открытый тренд: маппинг колонок и поток строк данных.

    Владеет открытым workbook; поток можно перезапустить (:meth:`rows`),
    но читать одновременно из двух итераторов по одному листу нельзя.
    """

    path: str
    size_bytes: int
    columns: List[MappedColumn]
    unmatched: List[str]
    ambiguous: List[str]
    header_rows: int
    t0: Optional[datetime] = None  # метка первой строки данных; известна уже при открытии
    _workbook: Any = field(repr=False, default=None)
    _sheet: Any = field(repr=False, default=None)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def rows(self) -> Iterator[Tuple[int, Dict[str, Any]]]:
        """Строки данных как ``(at_ms, {signal_id: value})``.

        ``at_ms`` отсчитывается от первой строки данных; строки с пустой или
        неразбираемой меткой пропускаются, время без монотонности не
        исправляется (плейбэк отбрасывает откат назад).
        """
        assert self._sheet is not None
        reset = getattr(self._sheet, "reset", None)
        if reset is not None:
            reset()  # read_only: пересмотреть файл с начала для нового прохода
        rows = self._sheet.iter_rows()
        for _ in range(self.header_rows):
            next(rows, None)
        t0: Optional[datetime] = None
        last_ms = -1
        by_index = {col.index: col for col in self.columns}
        for row in rows:
            cells = [c.value for c in row]
            moment = _parse_ts(cells[0] if cells else None)
            if moment is None:
                continue
            if t0 is None:
                t0 = moment
                self.t0 = moment
            at_ms = int(round((moment - t0).total_seconds() * 1000.0))
            if at_ms < 0 or at_ms < last_ms:
                continue  # немонотонная метка — пропуск, а не перескок времени
            last_ms = at_ms
            values: Dict[str, Any] = {}
            for index, col in by_index.items():
                value = _coerce(col.signal_type, cells[index] if index < len(cells) else None)
                if value is not None:
                    values[col.signal_id] = value
            yield at_ms, values

    def close(self) -> None:
        if self._workbook is not None:
            try:
                self._workbook.close()
            finally:
                self._workbook = None
                self._sheet = None


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.strptime(str(value).strip(), _TS_FORMAT)
    except ValueError:
        return None


def _norm(segment: str) -> str:
    return re.sub(r"[^a-z0-9]", "", segment.lower())


def _path_key(path: str) -> Optional[Tuple[str, str]]:
    """Ключ пути: два последних непустых сегмента в нормализованном виде."""
    segments = [_norm(s) for s in path.split(".") if s]
    if len(segments) < 2:
        return None
    return segments[-2], segments[-1]


def _coerce(signal_type: str, value: Any) -> Any:
    """Значение ячейки к типу сигнала; пропуск (None) для пустых и nan.

    Типовая таблица одна — signal_types (ТЗ #5 §11), контракт ``file``:
    Double больше не усекается до целого, дробное вне целого типа — пропуск
    ячейки (сигнал остаётся), а не усечённое неверное значение.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text.lower() == "nan":
            return None
    try:
        return signal_types.coerce(signal_type, value, contract="file")
    except (signal_types.SignalValueError, signal_types.SignalConfigError):
        return None


def open_source(
    path: str,
    configs: Mapping[str, Mapping[str, Any]],
    *,
    max_bytes: int,
) -> XlsxSource:
    """Открыть тренд и сопоставить колонки с опубликованными сигналами.

    ``configs`` — конфигурация сигналов имитатора (id, тип, node_id/address).
    Полное чтение не выполняется: заголовки читаются до первой строки данных
    (её метка становится ``t0`` источника), дальше строки отдаёт
    :meth:`XlsxSource.rows`.
    """
    if not os.path.isfile(path):
        raise PlaybackSourceError("not_found", f"Файл не найден: {path}")
    size = os.path.getsize(path)
    if size > max_bytes:
        raise PlaybackSourceError(
            "too_large",
            f"Файл {size} байт превышает лимит плейбэка {max_bytes} "
            "(PLAYBACK_MAX_BYTES)",
        )

    # Ключ пути -> (signal_id, type); повтор ключа — неоднозначность каналов.
    by_key: Dict[Tuple[str, str], Tuple[str, str]] = {}
    ambiguous: set = set()
    for signal_id, cfg in configs.items():
        node = cfg.get("node_id") or cfg.get("address") or signal_id
        key = _path_key(node)
        if key is None:
            continue
        if key in by_key and by_key[key][0] != signal_id:
            ambiguous.add(key)
            continue
        by_key[key] = (signal_id, cfg.get("type", "Boolean"))

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows()

        header_rows = 0
        best_path_row: List[str] = []
        first_ts: Optional[datetime] = None
        for row in rows:
            texts = [str(c.value) if c.value is not None else "" for c in row]
            moment = _parse_ts(texts[0] if texts else None)
            if moment is not None:
                first_ts = moment
                break  # первая строка данных — шапка кончилась (без неё)
            header_rows += 1
            dotted = sum(
                1 for t in texts if "." in t and not t.lstrip().startswith("[")
            )
            if dotted >= len(best_path_row) and dotted > 0:
                best_path_row = texts
        if not best_path_row:
            raise PlaybackSourceError(
                "no_paths", f"{os.path.basename(path)}: в шапке не найдено путей сигналов"
            )

        columns: List[MappedColumn] = []
        unmatched: List[str] = []
        taken: set = set()
        for index, text in enumerate(best_path_row):
            if not text or text.lower() == "time":
                continue
            key = _path_key(text)
            if key is None or key not in by_key:
                unmatched.append(text)
                continue
            if key in ambiguous:
                continue  # неоднозначные каналы не угадываем — молча не берём
            signal_id, signal_type = by_key[key]
            if signal_id in taken:
                unmatched.append(text)
                continue
            taken.add(signal_id)
            columns.append(MappedColumn(index=index, signal_id=signal_id, signal_type=signal_type))

        return XlsxSource(
            path=path,
            size_bytes=size,
            columns=columns,
            unmatched=unmatched,
            ambiguous=sorted(key[1] for key in ambiguous),
            header_rows=header_rows,
            t0=first_ts,
            _workbook=workbook,
            _sheet=sheet,
        )
    except Exception:
        workbook.close()
        raise
