"""Владение сигналами (ТЗ #5 §9.2): реестр и проверки шины.

Реестр — приобретение «всё или ничего» с полным перечнем конфликтов;
шина — чужая запись в занятый сигнал отклоняется (код ``owned``), сам
владелец пишет свободно.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

from app.services.opc_bus import OPCMode, OPCSignalBus
from app.services.ownership import Owner, OwnershipRegistry
from app.services.signal_cache import SignalCache

# Грабли #4: шина держит asyncio.Lock — один цикл на модуль, не asyncio.run.
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


CONFIGS = [
    {"id": "sig-a", "name": "A", "type": "Boolean", "writable": True, "default": False},
    {"id": "sig-b", "name": "B", "type": "Boolean", "writable": True, "default": False},
    {"id": "sig-c", "name": "C", "type": "Boolean", "writable": True, "default": False},
]

ALICE = Owner(id="scenario:aaa111", kind="scenario", title="Сценарий А")
BOB = Owner(id="scenario:bbb222", kind="scenario", title="Сценарий Б")


def _bus(registry):
    bus = OPCSignalBus(SignalCache(CONFIGS), mode=OPCMode.IMITATOR)
    bus.set_ownership(registry)
    return bus


# -- реестр -------------------------------------------------------------------


def test_acquire_is_all_or_nothing_with_the_full_conflict_list():
    registry = OwnershipRegistry()
    assert registry.acquire(ALICE, ["sig-a", "sig-b"]) == []

    conflicts = registry.acquire(BOB, ["sig-a", "sig-b"])
    assert {conflict.signal_id for conflict in conflicts} == {"sig-a", "sig-b"}
    assert all(conflict.owner.id == "scenario:aaa111" for conflict in conflicts)
    # «всё или ничего»: у Боба не осталось ничего
    assert registry.owner_of("sig-b") == ALICE


def test_same_owner_reacquires_freely_and_disjoint_sets_coexist():
    registry = OwnershipRegistry()
    assert registry.acquire(ALICE, ["sig-a"]) == []
    # тот же владелец повторно заявляет больше — конфликта нет
    assert registry.acquire(ALICE, ["sig-a", "sig-b"]) == []
    # непересекающийся набор другого владельца живёт одновременно
    assert registry.acquire(BOB, ["sig-c"]) == []
    held = registry.held_signals()
    assert held == {"sig-a": ALICE, "sig-b": ALICE, "sig-c": BOB}
    assert {owner.id for owner in registry.active_owners()} == {
        "scenario:aaa111",
        "scenario:bbb222",
    }


def test_release_frees_only_that_owner_signals():
    registry = OwnershipRegistry()
    registry.acquire(ALICE, ["sig-a", "sig-b"])
    registry.acquire(BOB, ["sig-c"])
    assert registry.release("scenario:aaa111") == ["sig-a", "sig-b"]
    assert registry.owner_of("sig-a") is None
    assert registry.owner_of("sig-b") is None
    assert registry.owner_of("sig-c") == BOB
    assert registry.release("scenario:aaa111") == []  # повторно — пусто


# -- шина ---------------------------------------------------------------------


def test_bus_rejects_foreign_write_but_the_owner_writes():
    registry = OwnershipRegistry()
    registry.acquire(ALICE, ["sig-a"])
    bus = _bus(registry)

    assert run(bus.write_value("sig-a", True, source="manual")) is False
    assert bus.cache.get_value("sig-a") is False  # чужая запись не применилась
    assert (
        run(bus.write_value("sig-a", True, source="scenario:aaa111", owner="scenario:aaa111"))
        is True
    )
    assert bus.cache.get_value("sig-a") is True  # владелец пишет свободно


def test_apply_changes_rejects_owned_signals_whole():
    registry = OwnershipRegistry()
    registry.acquire(ALICE, ["sig-a"])
    bus = _bus(registry)

    result = run(
        bus.apply_changes(
            {"sig-b": True, "sig-a": True}, source="manual", require_writable=True
        )
    )
    assert result["status"] == "rejected"
    assert result["applied"] == []
    assert result["results"]["sig-a"]["code"] == "owned"
    assert "Сценарий А" in result["results"]["sig-a"]["error"]
    assert "scenario:aaa111" in result["results"]["sig-a"]["error"]
    # прошедший проверку сосед помечен «не применён», значение не менялось
    assert result["results"]["sig-b"]["code"] == "package_rejected"
    assert bus.cache.get_value("sig-a") is False and bus.cache.get_value("sig-b") is False


def test_owner_signature_passes_apply():
    registry = OwnershipRegistry()
    registry.acquire(ALICE, ["sig-a"])
    bus = _bus(registry)

    result = run(
        bus.apply_changes(
            {"sig-a": True},
            source="scenario:aaa111",
            owner="scenario:aaa111",
            require_writable=True,
        )
    )
    assert result["status"] == "applied" and result["applied"] == ["sig-a"]


def test_unowned_write_to_owned_signal_is_rejected_too():
    """Неподписанный писец (manual/state_load без owner) в занятый сигнал не
    проходит: занятый сигнал меняет только владелец. Зазор остаётся только у
    heartbeat, который пишет в embedded-сервер мимо шины (перехват — шаг 7)."""
    registry = OwnershipRegistry()
    registry.acquire(ALICE, ["sig-a"])
    bus = _bus(registry)
    assert run(bus.write_value("sig-a", True, source="feed")) is False
    # незанятый сигнал неподписанному доступен
    assert run(bus.write_value("sig-b", True, source="feed")) is True
