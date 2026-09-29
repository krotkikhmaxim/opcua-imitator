"""Воспроизводимые режимы из реальных записей телеметрии.

Данные проверяются как данные: каждый режим проигрывается в виртуальном
времени, изменения не выходят из окна записи, маппятся только на
опубликованные сигналы, таймстампы следуют за моментом сценария. Ошибки
маппинга (сигнал не из конфигурации) режутся на загрузке.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

import pytest
from fastapi.testclient import TestClient

from app.config import load_signal_config
from app.main import app
from app.services.scenario import ScenarioEngine, ScenarioError, load_recorded_modes

CONFIG = load_signal_config()
IDS = [cfg["id"] for cfg in CONFIG]
MODES = load_recorded_modes(config=CONFIG)

#: шаг записи режимов — 10 мс (в отличие от синтетической модели, 100 мс).
REC_TICK = 10
#: сигналы, которые записи в принципе не воспроизводят (дискретные входы,
#: ШВП/ПУ), — для контроля, что режим не выдумывает их из слова-агрегата.
#: Здесь не проверяем состав, только принадлежность к публикуемому набору.


def _mode(mode_id):
    return MODES[mode_id]


def test_five_modes_are_loaded():
    assert {"in_motion", "charged_stopped", "machine_off", "drive_fault", "estop_pressed"} == set(MODES)


@pytest.mark.parametrize("mode_id", list(MODES))
def test_every_change_maps_to_a_published_signal(mode_id):
    mode = _mode(mode_id)
    assert mode.initial and all(sig in IDS for sig in mode.initial)
    assert all(change.signal_id in IDS for change in mode.changes)


@pytest.mark.parametrize("mode_id", list(MODES))
def test_changes_are_sorted_within_the_recording_window(mode_id):
    mode = _mode(mode_id)
    at = [change.at_ms for change in mode.changes]
    assert at == sorted(at)
    assert all(0 <= ms <= mode.duration_ms for ms in at)
    assert all(ms % REC_TICK == 0 for ms in at)


def test_fault_modes_are_flagged_and_benign_are_not():
    assert _mode("drive_fault").fault is True
    assert _mode("estop_pressed").fault is True
    assert _mode("in_motion").fault is False
    assert _mode("charged_stopped").fault is False
    assert _mode("machine_off").fault is False


# -- движок на виртуальном времени -------------------------------------------


class _VirtualTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _engine(tmp_path, writes, configs=CONFIG, simulated=True):
    clock = _VirtualTime()

    async def writer(signal_id, value, when):
        writes.append((signal_id, value, when))
        return True

    return ScenarioEngine(
        writer,
        configs,
        simulated=simulated,
        journal_path=tmp_path / "journal.jsonl",
        sleep=clock.sleep,
        clock=lambda: T0,
        monotonic=clock.monotonic,
    )


def test_engine_replays_a_mode_with_scenario_timestamps(tmp_path):
    writes = []
    engine = _engine(tmp_path, writes)
    mode = _mode("drive_fault")

    async def run():
        await engine.start(mode="drive_fault", seed=1)
        await engine._task

    asyncio.run(run())

    initial = writes[: len(mode.initial)]
    assert {signal_id for signal_id, _, _ in initial} == set(mode.initial)
    assert all(when == T0 for _, _, when in initial)
    stamp_fields = [when for _, _, when in writes]
    assert stamp_fields == sorted(stamp_fields)
    assert all((when - T0) % timedelta(milliseconds=REC_TICK) == timedelta(0) for when in stamp_fields)
    assert len(writes) == len(mode.initial) + len(mode.changes)
    status = engine.status()
    assert status["phase"] == "finished" and status["kind"] == "drive_fault"
    assert status["write_failures"] == 0

    (record,) = engine.journal()
    assert record["kind"] == "drive_fault" and record["fault"] is True
    assert record["expected_answer"]
    assert (tmp_path / "journal.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_engine_last_frame_is_written(tmp_path):
    """Конечное значение в журнал/шину — из конца записи, а не из начала."""
    writes = []
    engine = _engine(tmp_path, writes)
    mode = _mode("drive_fault")

    async def run():
        await engine.start(mode="drive_fault", seed=1)
        await engine._task

    asyncio.run(run())
    last_snapshot = {}
    for signal_id, value, _ in writes:
        last_snapshot[signal_id] = value
    # у drive_fault слово рекуператора в конце меняется 2 -> 8+/10 (есть динамика)
    assert last_snapshot["profibus-rec-status-word"] != mode.initial["profibus-rec-status-word"]


def test_unknown_mode_is_rejected(tmp_path):
    engine = _engine(tmp_path, [])

    async def run():
        with pytest.raises(ScenarioError) as info:
            await engine.start(mode="no_such_mode")
        assert info.value.code == "unknown_mode"

    asyncio.run(run())


def test_recorded_mode_refuses_the_real_mode(tmp_path):
    engine = _engine(tmp_path, [], simulated=False)

    async def run():
        with pytest.raises(ScenarioError) as info:
            await engine.start(mode="drive_fault")
        assert info.value.code == "real_mode"

    asyncio.run(run())


# -- HTTP API -----------------------------------------------------------------


def test_api_lists_the_recorded_modes():
    client = TestClient(app)
    body = client.get("/api/scenarios").json()
    assert {item["mode"] for item in body["modes"]} == set(MODES)
    assert all(item["signals"] for item in body["modes"])


def test_api_rejects_an_unknown_mode():
    client = TestClient(app)
    response = client.post("/api/scenarios/start", json={"mode": "no_such_mode"})
    assert response.status_code == 400