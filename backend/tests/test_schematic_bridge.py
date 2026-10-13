"""Мост DT→сигналы: извлечение обозначения, проценты листа, честный unmatched.

Ядро (``build_bridge``/``designator_of``) проверяется на синтетических
биндингах и designators: проценты считаются от размера листа, суффикс канала
снимается только когда прямое совпадение не нашлось, сигналы без обозначения
попадают в unmatched с причиной. Детерминизм: два прогона на одних входах —
равные тела.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from build_schematic_bridge import build_bridge, designator_of, load_designators


def _sheet(number, designators, width=596.0, height=843.0):
    return {
        "number": number,
        "width": width,
        "height": height,
        "designators": [{"text": t, "x": x, "y": y} for t, (x, y) in designators.items()],
    }


# -- извлечение DT -----------------------------------------------------------


def test_designator_of_reads_trailing_tag_component():
    assert designator_of("Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ1_SinhLeftTop") == "SQ1"
    assert designator_of("Chanel1.Application.GVL.DriveGVL.ProfibusData.CUCurrentFaultID") is None
    assert designator_of("Application.x10A6DigitalOutput.AH3_10A6_DO10") == "AH3"


# -- designators листов -------------------------------------------------------


def test_load_designators_converts_points_to_percent(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "sheet_57.json").write_text(
        json.dumps(_sheet(57, {"-U20": (298.0, 421.5)})), encoding="utf-8"
    )
    found = load_designators(pages)
    assert found["U20"] == {57: (50.0, 50.0)}


def test_load_designators_keeps_first_point_per_sheet(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    body = _sheet(1, {})
    body["designators"] = [
        {"text": "K1", "x": 10.0, "y": 10.0},
        {"text": "K1", "x": 90.0, "y": 90.0},
    ]
    (pages / "sheet_1.json").write_text(json.dumps(body), encoding="utf-8")
    assert load_designators(pages)["K1"] == {1: (round(10 / 596 * 100, 2), round(10 / 843 * 100, 2))}


# -- мост ---------------------------------------------------------------------

DESIGNATORS = {
    "U20": {57: (50.0, 10.0)},
    "SQ3": {75: (20.0, 30.0), 149: (25.0, 35.0)},
    "K1": {57: (60.0, 70.0)},
}

BINDINGS = [
    {"id": "ch1-10a3-u20", "project_tag": "Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.U20Ch1_Power"},
    {"id": "ch1-10a3-sq3", "project_tag": "Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ3_Top"},
    {"id": "ch1-10a4-u23-no", "project_tag": "Chanel1.Application.GVL.GlobalInOutSignal.x10A4DigitalInput.U23Ch1_NO"},
    {"id": "ch1-10a3-sq1", "project_tag": "Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ1_SinhLeftTop"},
    {"id": "profibus-inv-status-word", "project_tag": "Chanel1.Application.GVL.DriveGVL.ProfibusData.InvStatusWord"},
]


def test_bridge_matches_directly_and_via_channel_suffix():
    bridge = build_bridge(BINDINGS, DESIGNATORS)
    devices = bridge["devices"]
    # U20Ch1 напечатан как U20 — суффикс канала снят
    assert devices["U20"]["signals"] == ["ch1-10a3-u20"]
    # прямое совпадение с двумя листами — оба с первой точкой каждого
    assert devices["SQ3"]["sheets"] == {"75": [20.0, 30.0], "149": [25.0, 35.0]}
    assert "K1" not in devices  # K1 есть на листах, но нет среди сигналов


def test_bridge_unmatched_is_honest_about_reasons():
    bridge = build_bridge(BINDINGS, DESIGNATORS)
    reasons = {u["signal_id"]: (u["reason"], u["dt"]) for u in bridge["unmatched_signals"]}
    # SQ1 — обозначение в теге есть, на листах не нарисовано
    assert reasons["ch1-10a3-sq1"] == ("not_on_sheets", "SQ1")
    # profibus — хвост без DT вовсе
    assert reasons["profibus-inv-status-word"] == ("no_designator", None)
    # U23Ch1: после снятия суффикса U23 на листах нет — остаётся unmatched,
    # и записан извлечённый DT как есть, а не результат попытки
    assert reasons["ch1-10a4-u23-no"] == ("not_on_sheets", "U23Ch1")


def test_bridge_coverage_counts():
    bridge = build_bridge(BINDINGS, DESIGNATORS)
    cov = bridge["coverage"]
    assert cov["signals_total"] == 5
    assert cov["signals_matched"] == 2  # u20 и sq3
    assert cov["devices"] == 2
    assert cov["sheets"] == 3


def test_bridge_is_deterministic():
    assert build_bridge(BINDINGS, DESIGNATORS) == build_bridge(BINDINGS, DESIGNATORS)
