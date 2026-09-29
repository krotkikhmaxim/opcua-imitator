"""Воспроизводимые режимы из реальных записей телеметрии.

Каждый режим — компактный сжатый файл ``recordings/<mode>.gz`` в формате:

    строка 0: JSON-заголовок (метаданные, начальное состояние, маркеры события)
    далее:   строки "at_ms|signal_id|значение" — только смены значения.

Значение в строке — целое, либо ``b1``/``b0`` для логического сигнала (тип
сигнала не хранится, чтобы не расходиться с конфигурацией). Заголовок хранит
начальное состояние с настоящими типами (JSON). Загрузчик фильтрует сигналы,
которых нет в конфигурации имитатора, и при необходимости приводит тип к
конфигурации приложения.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .hoist import CauseChange
from .timeline import Change

#: Каталог данных по умолчанию — рядом с этим модулем.
DATA_DIR = Path(__file__).resolve().parent / "recordings"

_TYPES = {
    "Boolean": True,
    "Int16": False,
    "UInt16": False,
}


def _parse_line(line: str) -> Tuple[int, str, Any]:
    at_ms, signal_id, raw = line.rstrip("\n").split("|", 2)
    if raw == "b1":
        value: Any = True
    elif raw == "b0":
        value = False
    else:
        value = int(raw)
    return int(at_ms), signal_id, value


def _coerce_to_config(signal_id: str, value: Any, config_type: Optional[str]) -> Any:
    """Привести значение к типу конфигурации (Boolean vs Int16/UInt16)."""
    if config_type == "Boolean":
        return bool(value)
    if not isinstance(value, bool):
        return value
    return 1 if value else 0


@dataclass(frozen=True)
class RecordedMode:
    """Один воспроизводимый режим: начальное состояние + шкала изменений."""

    id: str
    title: str
    fault: bool
    description: str
    expected_answer: str
    source: str
    sha256: str
    recorded_at_first: str
    duration_ms: int
    cause_at_ms: int
    stopped_at_ms: int
    recovered_at_ms: int
    cause_visible_after_stop: bool
    causes: Tuple[CauseChange, ...]
    initial: Mapping[str, Any]
    changes: Tuple[Change, ...]


def _load_one(
    path: Path, config: Iterable[Mapping[str, Any]]
) -> RecordedMode:
    known = {cfg["id"]: cfg for cfg in config}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        header_line = f.readline()
        lines = f.readlines()
    header = json.loads(header_line)
    mode_id = header["mode_id"]

    def typed(value: Any, signal_id: str) -> Any:
        return _coerce_to_config(signal_id, value, known.get(signal_id, {}).get("type"))

    initial = {sid: typed(v, sid) for sid, v in header["initial"].items() if sid in known}
    causes = tuple(
        CauseChange(
            c["signal_id"],
            None if c["before"] is None else typed(c["before"], c["signal_id"]),
            typed(c["after"], c["signal_id"]),
        )
        for c in header["causes"]
        if c["signal_id"] in known
    )
    changes: List[Change] = []
    for line in lines:
        if not line.strip():
            continue
        at_ms, signal_id, value = _parse_line(line)
        if signal_id not in known:
            continue
        changes.append(Change(at_ms, signal_id, typed(value, signal_id)))
    changes.sort(key=lambda c: (c.at_ms, c.signal_id))
    return RecordedMode(
        id=mode_id,
        title=header.get("title", mode_id),
        fault=bool(header.get("fault", False)),
        description=header.get("description", ""),
        expected_answer=header.get("expected_answer", ""),
        source=header.get("source", path.name),
        sha256=header.get("sha256", ""),
        recorded_at_first=header.get("recorded_at_first", ""),
        duration_ms=int(header.get("duration_ms", 0)),
        cause_at_ms=int(header.get("cause_at_ms", 0)),
        stopped_at_ms=int(header.get("stopped_at_ms", 0)),
        recovered_at_ms=int(header.get("recovered_at_ms", 0)),
        cause_visible_after_stop=bool(header.get("cause_visible_after_stop", False)),
        causes=causes,
        initial=initial,
        changes=tuple(changes),
    )


def load_recorded_modes(
    data_dir: Path = DATA_DIR, config: Optional[Iterable[Mapping[str, Any]]] = None
) -> Dict[str, RecordedMode]:
    """Загрузить все режимы из ``data_dir``; метаданные (title/fault/ответ) — из attach/manifest."""

    manifest: List[Dict[str, Any]] = []
    manifest_path = data_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    meta = {item["mode"]: item for item in manifest}

    config = config or []
    modes: Dict[str, RecordedMode] = {}
    for path in sorted(data_dir.glob("*.gz")):
        mode_id = path.name[: -len(".gz")]
        mode = _load_one(path, config)
        info = meta.get(mode_id, {})
        mode = RecordedMode(
            id=mode.id,
            title=info.get("title", mode.title),
            fault=bool(info.get("fault", mode.fault)),
            description=info.get("description", mode.description),
            expected_answer=info.get("expected_answer", mode.expected_answer),
            source=mode.source,
            sha256=mode.sha256,
            recorded_at_first=mode.recorded_at_first,
            duration_ms=mode.duration_ms,
            cause_at_ms=mode.cause_at_ms,
            stopped_at_ms=mode.stopped_at_ms,
            recovered_at_ms=mode.recovered_at_ms,
            cause_visible_after_stop=mode.cause_visible_after_stop,
            causes=mode.causes,
            initial=mode.initial,
            changes=mode.changes,
        )
        modes[mode_id] = mode
    return modes