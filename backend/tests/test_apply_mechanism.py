"""Общий механизм применения и подтверждения (ТЗ #5, шаг 4 — §6, §7).

Уровень шины: порядок пакет-проверка→применение, дедупликация по op_id,
оптимистичная ревизия, частичное выполнение только от отказа записи.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import load_signal_config
from app.services.opc_bus import APPLY_DEDUP_WINDOW, OPCMode, OPCSignalBus
from app.services.signal_cache import SignalCache

BOOL_SIG = "ch1-10a3-sq1"
INT_SIG = "profibus-motor-speed"  # Int16


@pytest.fixture()
def bus() -> OPCSignalBus:
    return OPCSignalBus(SignalCache(load_signal_config()), mode=OPCMode.IMITATOR)


# Один цикл на модуль: _apply_lock шины привязывается к первому циклу, и
# разные asyncio.run() на каждый вызов ломали бы её повторное использование.
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


class TestPackageValidation:
    def test_applied_updates_state_and_revision(self, bus):
        result = run(bus.apply_changes({BOOL_SIG: True}, source="manual", op_id="op-1"))
        assert result["status"] == "applied"
        assert result["results"][BOOL_SIG]["revision"] == 1
        state = bus.cache.get_state(BOOL_SIG)
        assert state.value is True
        assert state.source == "manual"
        assert state.changed_at is not None

    def test_unknown_signal_rejects_whole_package(self, bus):
        # §7: до записи — ничего не применяется, хорошее значение тоже.
        result = run(bus.apply_changes(
            {BOOL_SIG: True, "nope": 1}, source="manual"))
        assert result["status"] == "rejected"
        assert result["applied"] == []
        assert bus.cache.get_value(BOOL_SIG) is False
        assert result["results"]["nope"]["code"] == "unknown_signal"
        assert result["results"][BOOL_SIG]["code"] == "package_rejected"

    def test_invalid_value_rejects_package_without_coercion(self, bus):
        result = run(bus.apply_changes({INT_SIG: 1.5}, source="manual", contract="api"))
        assert result["status"] == "rejected"
        assert result["results"][INT_SIG]["code"] == "invalid_value"

    def test_not_writable_only_with_flag(self, bus):
        # Без флага (сценарные источники) право на запись не проверяется;
        # ручные источники передают require_writable=True.
        result = run(bus.apply_changes({BOOL_SIG: True}, source="scenario"))
        assert result["status"] == "applied"
        result = run(bus.apply_changes(
            {BOOL_SIG: True}, source="manual", require_writable=True))
        assert result["status"] == "applied"


class TestRevisions:
    def test_expected_revision_conflict_rejects(self, bus):
        run(bus.apply_changes({BOOL_SIG: True}, source="manual"))
        current = bus.cache.get_revision(BOOL_SIG)
        result = run(bus.apply_changes(
            {BOOL_SIG: False}, source="manual", expected_revisions={BOOL_SIG: current - 1}))
        assert result["status"] == "rejected"
        assert result["results"][BOOL_SIG]["code"] == "revision_conflict"
        # Значение осталось от подтверждённой записи.
        assert bus.cache.get_value(BOOL_SIG) is True

    def test_expected_revision_match_applies(self, bus):
        run(bus.apply_changes({BOOL_SIG: True}, source="manual"))
        current = bus.cache.get_revision(BOOL_SIG)
        result = run(bus.apply_changes(
            {BOOL_SIG: False}, source="manual", expected_revisions={BOOL_SIG: current}))
        assert result["status"] == "applied"
        assert bus.cache.get_value(BOOL_SIG) is False

    def test_revision_grows_with_each_apply_not_with_read(self, bus):
        run(bus.apply_changes({BOOL_SIG: True}, source="manual"))
        first = bus.cache.get_revision(BOOL_SIG)
        run(bus.read_value(BOOL_SIG))
        run(bus.read_value(BOOL_SIG))
        assert bus.cache.get_revision(BOOL_SIG) == first
        run(bus.apply_changes({BOOL_SIG: False}, source="manual"))
        assert bus.cache.get_revision(BOOL_SIG) == first + 1


class TestDeduplication:
    def test_same_op_id_same_body_replays_without_applying(self, bus):
        first = run(bus.apply_changes({BOOL_SIG: True}, source="manual", op_id="dup-1"))
        assert first["status"] == "applied"
        revision = bus.cache.get_revision(BOOL_SIG)
        second = run(bus.apply_changes({BOOL_SIG: True}, source="manual", op_id="dup-1"))
        assert second["replayed"] is True
        assert second["status"] == "applied"
        assert second["results"] == first["results"]
        assert bus.cache.get_revision(BOOL_SIG) == revision  # повтор не применялся

    def test_same_op_id_other_body_is_conflict(self, bus):
        run(bus.apply_changes({BOOL_SIG: True}, source="manual", op_id="dup-2"))
        result = run(bus.apply_changes({BOOL_SIG: False}, source="manual", op_id="dup-2"))
        assert result["status"] == "conflict"
        assert result["code"] == "op_id_conflict"
        assert bus.cache.get_value(BOOL_SIG) is True

    def test_rejected_command_is_remembered(self, bus):
        first = run(bus.apply_changes({"nope": 1}, source="manual", op_id="dup-3"))
        assert first["status"] == "rejected"
        second = run(bus.apply_changes({"nope": 1}, source="manual", op_id="dup-3"))
        assert second["replayed"] is True

    def test_window_keeps_last_commands_only(self, bus):
        for i in range(APPLY_DEDUP_WINDOW):
            run(bus.apply_changes({INT_SIG: i}, source="manual", op_id=f"bulk-{i}"))
        # Окно держит ровно последние N: оба края живы.
        edge = run(bus.apply_changes({INT_SIG: 0}, source="manual", op_id="bulk-0"))
        assert edge["replayed"] is True
        # Ещё одна команда вытесняет старейшую — bulk-0 вне окна, это новая.
        run(bus.apply_changes({INT_SIG: 0}, source="manual", op_id="bulk-new"))
        fresh = run(bus.apply_changes({INT_SIG: 0}, source="manual", op_id="bulk-0"))
        assert "replayed" not in fresh

    def test_generated_op_id_unique(self, bus):
        first = run(bus.apply_changes({BOOL_SIG: True}, source="manual"))
        second = run(bus.apply_changes({BOOL_SIG: True}, source="manual"))
        assert first["op_id"] != second["op_id"]


class TestPartialApply:
    def test_write_failure_gives_partial_without_rollback(self, bus, monkeypatch):
        # §7: начавший применяться пакет отчитывается по каждому сигналу;
        # автоматический откат не выполняется.
        async def broken_write(signal_id, value, source=None):
            return False

        monkeypatch.setattr(bus, "write_value", broken_write)
        result = run(bus.apply_changes(
            {BOOL_SIG: True, INT_SIG: 1200}, source="manual"))
        assert result["status"] == "failed"
        assert result["applied"] == []
        assert result["results"][BOOL_SIG]["code"] == "write_failed"
        assert bus.cache.get_value(BOOL_SIG) is False

    def test_second_signal_failure_gives_partial(self, bus, monkeypatch):
        calls = {"n": 0}

        async def flaky_write(signal_id, value, source=None):
            calls["n"] += 1
            return calls["n"] == 1  # первая запись удачна, вторая нет

        monkeypatch.setattr(bus, "write_value", flaky_write)
        result = run(bus.apply_changes(
            {BOOL_SIG: True, INT_SIG: 1200}, source="manual"))
        assert result["status"] == "partial"
        assert result["applied"] == [BOOL_SIG]
        assert result["results"][INT_SIG]["code"] == "write_failed"


class TestOrdering:
    def test_changes_apply_in_transmission_order(self, bus):
        # Словарь сохраняет порядок вставки; применение идёт в нём (§6).
        seen: list[str] = []
        original = bus.write_value

        async def spy(signal_id, value, source=None):
            seen.append(signal_id)
            return await original(signal_id, value, source=source)

        bus.write_value = spy  # type: ignore[assignment]
        run(bus.apply_changes({BOOL_SIG: True, INT_SIG: 5}, source="manual"))
        assert seen == [BOOL_SIG, INT_SIG]
