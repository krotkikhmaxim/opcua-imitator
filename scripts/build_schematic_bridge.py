"""Мост «обозначение Э3 → сигналы стенда» для SchematicView.

Читает биндинги стенда (id сигнала + project_tag) и designators листов
печатной схемы станции (sheet_*.json: text/x/y в пунктах листа) и строит
``backend/configs/schematic_bridge.json`` (схема ``stand.schematic-bridge.v1``):
на каждое обозначение аппарата (DT) — сигналы стенда и листы с процентными
координатами бейджа. Бейджи позиционируются процентами, поэтому размер листа
в мост не пишется — лист целиком фронт берёт у станции.

DT извлекается из хвостового компонента project_tag: ``…x10A3DigitalInput.
SQ1_SinhLeftTop`` → ``SQ1`` (буквы, затем цифры, до первого ``_``). Если
обозначения с таким текстом на листах нет, суффикс канала ``Ch1``/``Ch2``
снимается: PLC-имя ``U20Ch1`` напечатано как ``U20``. Что не сошлось —
перечисляется в ``unmatched_signals`` с причиной: profibus-слова и счётчики
оборотов без обозначения остаются unmatched честно.

Входы: биндинги стенда и каталог листов станции (``--station-root``).
Выход детерминирован входами: те же входы — те же байты (сортированные
ключи и списки, никаких временных меток).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = "stand.schematic-bridge.v1"

#: Хвостовой компонент тега: DT — буквы, затем цифры (у ``K200Ch1`` это K200Ch1;
#: у ``CUCurrentFaultID`` цифр нет — не DT).
_DT_RE = re.compile(r"^([A-Za-z]+[0-9]+[A-Za-z0-9]*)(?:_|$)")
#: Суффикс канала в PLC-имени, которого нет на печати.
_CHANNEL_SUFFIX_RE = re.compile(r"(?i)Ch[12]$")


def designator_of(tag: str) -> str | None:
    """DT из хвостового компонента пути тега или ``None``."""
    last = tag.split(".")[-1]
    match = _DT_RE.match(last)
    return match.group(1) if match else None


def load_designators(pages_dir: Path) -> dict[str, dict[int, tuple[float, float]]]:
    """``текст → {номер листа: (x_pct, y_pct)}`` по всем sheet_*.json.

    Первый designator листа выигрывает: повтор обозначения на листе — одна
    и та же печать, а не вторая точка. Текст берётся после снятия ведущего
    ``-`` (конвенция EPLAN) и пробелов.
    """
    found: dict[str, dict[int, tuple[float, float]]] = {}
    for path in sorted(pages_dir.glob("sheet_*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        number = int(page["number"])
        width, height = float(page["width"]), float(page["height"])
        if width <= 0 or height <= 0:
            continue
        for point in page.get("designators") or []:
            text = str(point.get("text", "")).lstrip("-").strip()
            if not text:
                continue
            sheets = found.setdefault(text, {})
            if number in sheets:
                continue
            x_pct = round(float(point["x"]) / width * 100, 2)
            y_pct = round(float(point["y"]) / height * 100, 2)
            sheets[number] = (x_pct, y_pct)
    return found


def build_bridge(
    bindings: list[dict], designators: dict[str, dict[int, tuple[float, float]]]
) -> dict:
    """Тело артефакта: devices, unmatched_signals, coverage."""
    devices: dict[str, dict] = {}
    unmatched: list[dict] = []
    for binding in bindings:
        signal_id = str(binding.get("id", ""))
        tag = str(binding.get("project_tag") or "")
        dt = designator_of(tag)
        if dt is None:
            unmatched.append({"signal_id": signal_id, "reason": "no_designator", "dt": None})
            continue
        sheets = designators.get(dt)
        if sheets is None:
            stripped = _CHANNEL_SUFFIX_RE.sub("", dt)
            sheets = designators.get(stripped)
            if sheets is not None:
                dt = stripped
        if sheets is None:
            unmatched.append({"signal_id": signal_id, "reason": "not_on_sheets", "dt": dt})
            continue
        device = devices.setdefault(dt, {"signals": [], "sheets": {}})
        if signal_id not in device["signals"]:
            device["signals"].append(signal_id)
        for number, (x_pct, y_pct) in sheets.items():
            device["sheets"].setdefault(str(number), [x_pct, y_pct])
    for device in devices.values():
        device["signals"].sort()
    unmatched.sort(key=lambda item: (item["signal_id"],))
    matched = sum(len(d["signals"]) for d in devices.values())
    return {
        "schema": SCHEMA,
        "devices": {dt: devices[dt] for dt in sorted(devices)},
        "unmatched_signals": unmatched,
        "coverage": {
            "signals_total": len(bindings),
            "signals_matched": matched,
            "devices": len(devices),
            "sheets": len({n for d in devices.values() for n in d["sheets"]}),
        },
    }


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bindings",
        type=Path,
        default=repo / "docs" / "opcua_input_bindings.json",
        help="биндинги стенда (id + project_tag)",
    )
    parser.add_argument(
        "--station-root",
        type=Path,
        default=Path("/home/max/projects/asutpv1_bigllm"),
        help="корень чекаута станции (data/schematic/...)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repo / "backend" / "configs" / "schematic_bridge.json",
        help="куда писать артефакт",
    )
    args = parser.parse_args(argv)

    raw = json.loads(args.bindings.read_text(encoding="utf-8"))
    bindings = raw.get("bindings") if isinstance(raw, dict) else raw
    pages_dir = args.station_root / "data" / "schematic" / "eplan_124" / "pages"
    if not pages_dir.is_dir():
        print(f"каталога листов нет: {pages_dir}", file=sys.stderr)
        return 1

    designators = load_designators(pages_dir)
    bridge = build_bridge(bindings, designators)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(bridge, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cov = bridge["coverage"]
    print(
        f"мост: {cov['signals_matched']}/{cov['signals_total']} сигналов, "
        f"{cov['devices']} обозначений, {cov['sheets']} листов; "
        f"unmatched: {len(bridge['unmatched_signals'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
