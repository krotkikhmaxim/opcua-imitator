"""Чтение SQLite-архива станции: окно, склейка серий, лимит, read-only.

Фикстура создаёт архив с той же DDL, что станция
(``src/telemetry_ingestion/archive_store.py`` — текст взят из живого файла
дословно; пакет станции не импортируется). Ядро проверяется на малом
архиве: окно по observed_us отдаётся как поток изменений с t0 первой
отсчётки, серии одного сигнала склеиваются, bad-качество не играется,
окно сверх лимита отклоняется, а соединение write-запросом ломается.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))

from app.services.playback.archive_reader import (  # noqa: E402
    ArchiveError,
    ArchiveSource,
    list_series,
    open_archive,
)

# DDL станции (из живого архива; усечены только текстовые NOT NULL колонки
# серий, которые читатель не выбирает).
_DDL = """
CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE series (
    series_id INTEGER PRIMARY KEY,
    series_key TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    value_type TEXT NOT NULL,
    config_version TEXT NOT NULL,
    project_id TEXT,
    project_revision TEXT,
    telemetry_config_version TEXT,
    connection_id TEXT,
    binding_id TEXT,
    project_tag_id TEXT,
    symbol_id TEXT,
    browse_path TEXT
);
CREATE INDEX series_binding ON series (project_id, connection_id, binding_id);
CREATE TABLE readings (
    row INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL,
    sample_id BLOB NOT NULL,
    observed_us INTEGER NOT NULL, observed_tz INTEGER,
    source_us INTEGER NOT NULL, source_tz INTEGER,
    server_us INTEGER NOT NULL, server_tz INTEGER,
    received_us INTEGER NOT NULL, received_tz INTEGER,
    recorded_us INTEGER NOT NULL, recorded_tz INTEGER,
    ingested_us INTEGER, ingested_tz INTEGER,
    value,
    status_code TEXT NOT NULL,
    quality TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    source_freshness TEXT,
    transport_freshness TEXT,
    reason_code TEXT
);
CREATE INDEX readings_window ON readings (series_id, observed_us);
"""


def _build_archive(tmp_path: Path, series: list[dict], readings: list[tuple]) -> Path:
    """``series``: {id, signal_id, value_type}; ``readings``: (series_id, observed_us, value, quality)."""
    path = tmp_path / "archive.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    for spec in series:
        conn.execute(
            "INSERT INTO series (series_id, series_key, schema_version, asset_id, signal_id,"
            " endpoint_id, node_id, value_type, config_version)"
            " VALUES (?, ?, 'telemetry.archive_store.v1', 'asset', ?, 'ep', 'ns=2;s=x', ?, 'cfg')",
            (spec["id"], f"sha256:{spec['id']:064x}", spec["signal_id"], spec["value_type"]),
        )
    for i, (series_id, observed_us, value, quality) in enumerate(readings, start=1):
        conn.execute(
            "INSERT INTO readings (row, series_id, sample_id, observed_us, source_us, server_us,"
            " received_us, recorded_us, value, status_code, quality, sequence_no)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '0', ?, ?)",
            (i, series_id, b"\x00" * 16, observed_us, observed_us, observed_us,
             observed_us, observed_us, value, quality, i),
        )
    conn.commit()
    conn.close()
    return path


_SERIES = [
    {"id": 1, "signal_id": "shvp-u6", "value_type": "boolean"},
    {"id": 2, "signal_id": "shvp-k1", "value_type": "integer"},
]

_READINGS = [
    (1, 1_000_000, 1, "good"),     # 0 мс
    (2, 1_000_000, 5, "good"),     # 0 мс (другой сигнал)
    (1, 2_000_000, 0, "good"),     # 1000 мс
    (2, 3_500_000, 7, "good"),     # 2500 мс
    (1, 4_000_000, 1, "bad"),      # bad — не играется
    # второй серии того же сигнала shvp-u6 (партия записи)
    (1, 5_000_000, 0, "good"),     # 4000 мс
]


def _open(path: Path, **kwargs):
    kwargs.setdefault("max_changes", 10_000)
    return open_archive(str(path), **kwargs)


def test_rows_yield_changes_with_t0_of_window(tmp_path):
    src = _open(_build_archive(tmp_path, _SERIES, _READINGS))
    rows = list(src.rows())
    assert rows == [
        (0, {"shvp-u6": True}),
        (0, {"shvp-k1": 5}),
        (1000, {"shvp-u6": False}),
        (2500, {"shvp-k1": 7}),
        (4000, {"shvp-u6": False}),
    ]
    assert src.t0 is not None and src.t0.timestamp() == 1.0
    assert src.name.endswith("(архив)")
    src.close()


def test_series_of_one_signal_are_merged(tmp_path):
    path = _build_archive(
        tmp_path,
        [*_SERIES, {"id": 3, "signal_id": "shvp-u6", "value_type": "boolean"}],
        _READINGS,
    )
    src = _open(path, signal_ids=["shvp-u6"])
    rows = list(src.rows())
    assert all(list(values) == ["shvp-u6"] for _, values in rows)
    assert [at_ms for at_ms, _ in rows] == [0, 1000, 4000]
    src.close()


def test_window_bounds_are_inclusive(tmp_path):
    path = _build_archive(tmp_path, _SERIES, _READINGS)
    src = _open(path, start_us=2_000_000, end_us=3_500_000)
    assert list(src.rows()) == [
        (0, {"shvp-u6": False}),   # observed 2_000_000 — граница входит
        (1500, {"shvp-k1": 7}),    # 3_500_000 — граница входит
    ]
    src.close()


def test_over_limit_window_is_rejected_not_truncated(tmp_path):
    path = _build_archive(tmp_path, _SERIES, _READINGS)
    with raises_archive("too_many_changes") as info:
        _open(path, max_changes=3)
    assert "PLAYBACK_MAX_CHANGES" in str(info.value)


def test_bad_window_and_unknown_signal_are_rejected(tmp_path):
    path = _build_archive(tmp_path, _SERIES, _READINGS)
    with raises_archive("bad_window"):
        _open(path, start_us=10, end_us=1)
    with raises_archive("unknown_signals"):
        _open(path, signal_ids=["nope"])
    with raises_archive("empty_window"):
        _open(path, start_us=99_000_000, end_us=100_000_000)


def test_bad_quality_is_skipped_and_counted(tmp_path):
    path = _build_archive(tmp_path, _SERIES, _READINGS)
    src = _open(path)
    values = [v for _, values in src.rows() for v in values.values()]
    assert len(values) == 5  # один bad пропущен
    assert src.skipped_quality == 1
    src.close()


def test_connection_is_read_only(tmp_path):
    src = _open(_build_archive(tmp_path, _SERIES, _READINGS))
    try:
        try:
            src._conn.execute("INSERT INTO series (series_id, series_key, signal_id, value_type) VALUES (99, 'x', 'x', 'boolean')")
            written = True
        except sqlite3.OperationalError:
            written = False
        assert not written, "запись в архив прошла — mode=ro потерян"
    finally:
        src.close()


def test_list_series_merges_and_reports_span(tmp_path):
    path = _build_archive(
        tmp_path,
        [*_SERIES, {"id": 3, "signal_id": "shvp-u6", "value_type": "boolean"}],
        _READINGS,
    )
    items, meta = list_series(str(path))
    by_signal = {item.signal_id: item for item in items}
    assert by_signal["shvp-u6"].rows == 4  # две серии склеены
    assert by_signal["shvp-k1"].rows == 2
    assert (meta["observed_min_us"], meta["observed_max_us"]) == (1_000_000, 5_000_000)


class raises_archive:
    """``pytest.raises`` локально: ожидаемый код ArchiveError."""

    def __init__(self, code: str) -> None:
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            raise AssertionError(f"ожидали ArchiveError({self.code}), отказа не было")
        assert isinstance(exc, ArchiveError), f"чужое исключение: {exc!r}"
        assert exc.code == self.code, f"код {exc.code!r} != {self.code!r}"
        self.value = exc
        return True
