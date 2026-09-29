"""Импорт записей режимов (xlsx из krotkikhmaxim/temp) в компактные файлы данных.

Читает экспортированные из ПЛК записи режимов подъёмной машины и сводит
каждый режим к шкале изменений опубликованных сигналов: ``(at_ms, signal_id,
value)`` — только смены значения, как у OPC UA подписки. Данные ложатся в
``backend/app/services/scenario/recordings/<mode>.gz`` и проигрываются движком.

**Какие сигналы публикуются.** Только те, что маппятся из записи ОДНОЗНАЧНО
и подтверждены: прямые колонки привода (слова состояния, скорость, ток,
момент, коды ошибок) и дискретные входы, данные в записи напрямую (реле ТП
канала 1, входы канала 2 20A3). Слова ``StatusDword_<module>`` НЕ декодируются
в отдельные теги: раскладка битов записи для модулей 2A2/2A3/3A2/3A3 не
совпадает с биндингами (проверено: «КАР в нуле» и «КРТ расторможено» читаются
из слова панели как сработавшие даже в «машина отключена»), а единственный
перекрёстно подтверждённый модуль (10A3, бит 19/20 == прямые K1/K2) дублируется
прямыми колонками. Выкладывать непроверенную раскладку = учить станцию аварии,
которой в объекте нет.
"""
from __future__ import annotations

import glob as _glob
import gzip
import hashlib
import json
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import openpyxl

#: Точные заголовки колонок записи -> id сигнала в конфигурации имитатора.
#: Ни один из них не выводится из слова-агрегата: все даны записью напрямую.
DIRECT: Dict[str, str] = {
    "Application.Drive_GVL.ProfibusData.InvStatusWord.InvStatusWord": "profibus-inv-status-word",
    "Application.Drive_GVL.ProfibusData.InvStatusWord1.InvStatusWord1": "profibus-inv-status-word-1",
    "Application.Drive_GVL.ProfibusData.InvStatusWord2.InvStatusWord2": "profibus-inv-status-word-2",
    "Application.Drive_GVL.ProfibusData.RecStatusWord.RecStatusWord": "profibus-rec-status-word",
    "Application.Drive_GVL.ProfibusData.MotorSpeed": "profibus-motor-speed",
    "Application.Drive_GVL.ProfibusData.CurrFilted": "profibus-current-filtered",
    "Application.Drive_GVL.ProfibusData.OutputTorque": "profibus-output-torque",
    "Application.Drive_GVL.ProfibusData.CUCurrentFaultID": "profibus-cu-fault-id",
    "Application.Drive_GVL.ProfibusData.RecCurrentFaultID": "profibus-rec-fault-id",
    "Application.Drive_GVL.ProfibusData.InvCurrrentFaultID": "profibus-inv-fault-id",
    "Application.GlobalInOutSignal.x10A3DigitalInput.K1Ch1_FbRtp1": "ch1-10a3-k1",
    "Application.GlobalInOutSignal.x10A3DigitalInput.K2Ch1_FbRtp2": "ch1-10a3-k2",
    "Application.GlobalInOutSignal.x20A3DigitalInput.SQ1_SinhLeftTop": "ch2-20a3-sq1",
    "Application.GlobalInOutSignal.x20A3DigitalInput.SQ3_SinhRightTop": "ch2-20a3-sq3",
    "Application.GlobalInOutSignal.x20A3DigitalInput.SQ5_OwerWindLeft": "ch2-20a3-sq5",
    "Application.GlobalInOutSignal.x20A3DigitalInput.SQ6_OwerWindRight": "ch2-20a3-sq6",
    "Application.GlobalInOutSignal.x20A3DigitalInput.U10Ch2_FbBriz2": "ch2-20a3-u10",
    "Application.GlobalInOutSignal.x20A3DigitalInput.U11Ch2_FbBriz2": "ch2-20a3-u11",
    "Application.GlobalInOutSignal.x20A3DigitalInput.U12Ch2_FbBriz2": "ch2-20a3-u12",
    "Application.GlobalInOutSignal.x20A3DigitalInput.K2Ch2_FbRtp2": "ch2-20a3-k2",
}

#: Типы по id (Boolean/Int16/UInt16) — для приведения значения к типу OPC UA.
TYPES: Dict[str, str] = {
    "profibus-inv-status-word": "UInt16",
    "profibus-inv-status-word-1": "UInt16",
    "profibus-inv-status-word-2": "UInt16",
    "profibus-rec-status-word": "UInt16",
    "profibus-motor-speed": "Int16",
    "profibus-current-filtered": "Int16",
    "profibus-output-torque": "Int16",
    "profibus-cu-fault-id": "Int16",
    "profibus-rec-fault-id": "Int16",
    "profibus-inv-fault-id": "Int16",
}
for _b in (
    "ch1-10a3-k1", "ch1-10a3-k2",
    "ch2-20a3-sq1", "ch2-20a3-sq3", "ch2-20a3-sq5", "ch2-20a3-sq6",
    "ch2-20a3-u10", "ch2-20a3-u11", "ch2-20a3-u12", "ch2-20a3-k2",
):
    TYPES[_b] = "Boolean"

#: id режима: (файл, title, fault, ожидаемый ответ — текст для журнала правды).
MODES: List[Tuple[str, str, str, bool, str]] = [
    (
        "В движениии 2.xlsx",
        "in_motion",
        "В движении",
        False,
        "Штатный ход машины без аварий: привод ведёт (скорость двигателя ненулевая, "
        "слово состояния инвертора 0x0422 — функция и нормальная работа), реле ТП под "
        "током (K1=1), коды ошибок привода равны нулю.",
    ),
    (
        "Заряжен машина стоит на месте 2.xlsx",
        "charged_stopped",
        "Заряжен (машина стоит на месте)",
        False,
        "Машина заряжена и стоит на месте: скорость 0, привод готов (слово состояния "
        "инвертора 0x0401), реле ТП под током (K1=1), коды ошибок 0. Аварий нет.",
    ),
    (
        "машина отключена.xlsx",
        "machine_off",
        "Машина отключена",
        False,
        "Машина отключена: скорость 0, привод не готов к работе (слово состояния "
        "инвертора 0x0001), реле ТП снято (K1=0), слово рекуператора в «не готов». "
        "Обесточенное штатное состояние.",
    ),
    (
        "авария привода 2.xlsx",
        "drive_fault",
        "Авария привода",
        True,
        "Привод в аварийном состоянии: реле ТП снято (K1=0), скорость 0, слово состояния "
        "инвертора 0x0401 без признаков работы, слово рекуператора меняется 2→8→10 "
        "(событие в отрезке). Машина стоит.",
    ),
    (
        "нажата аварийная кнопка 2.xlsx",
        "estop_pressed",
        "Нажата аварийная кнопка",
        True,
        "Состояние аварийного останова: скорость 0, реле ТП снято (K1=0), слово состояния "
        "инвертора 0x0401 без признаков работы. Кнопка удерживается до конца записи.",
    ),
]


def _ts(value: Any) -> datetime:
    """Метка записи 'DD.MM.YYYY HH:MM:SS.ffffff' -> datetime (локальное время записи)."""
    return datetime.strptime(str(value), "%d.%m.%Y %H:%M:%S.%f")


def _coerce(signal_id: str, value: Any) -> Any:
    if TYPES.get(signal_id) == "Boolean":
        return bool(int(value) != 0)
    return int(value)


def convert(src: Path, mode_id: str) -> Dict[str, Any]:
    headers: List[str] = []
    rows: List[List[Any]] = []
    wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
    try:
        ws = wb.worksheets[0]
        it = iter(ws.iter_rows())
        next(it)  # первый заголовок ([0:0] ...)
        h2 = next(it)
        headers = [str(c.value) if c.value is not None else "" for c in h2]
        rows = [[c.value for c in r] for r in it]
    finally:
        wb.close()

    col_index: Dict[str, int] = {}
    for idx, name in enumerate(headers):
        if name in DIRECT and name not in col_index:
            col_index[name] = idx
    signals = [DIRECT[name] for name in col_index]
    mapped_by_signal = {signal_id: col_index[name] for name, signal_id in DIRECT.items() if name in col_index}

    if not signals:
        raise RuntimeError(f"{src.name}: не найдено ни одной маппируемой колонки")

    t0 = _ts(rows[0][0])
    last_value: Dict[str, Any] = {}
    first_change: Dict[str, Tuple[int, Any, Any]] = {}
    initial: Dict[str, Any] = {}
    changes: List[Tuple[int, str, Any]] = []
    duration_ms = 0

    for row in rows:
        raw_t = _ts(row[0])
        at_ms = int(round((raw_t - t0).total_seconds() * 1000.0))
        if at_ms < 0:
            continue
        frame = {sid: mapped_by_signal[sid] for sid in signals}
        for sid in signals:
            value = _coerce(sid, row[mapped_by_signal[sid]])
            prev = last_value.get(sid)
            if sid not in initial:
                initial[sid] = value
            if prev is None or prev != value:
                changes.append((at_ms, sid, value))
                first_change.setdefault(sid, (at_ms, prev, value))
                last_value[sid] = value
        if at_ms > duration_ms:
            duration_ms = at_ms

    changes.sort(key=lambda c: (c[0], list(signals).index(c[1])))
    first_ms = min((c[0] for c in changes), default=0)
    causes = [
        {"signal_id": sid, "before": before, "after": after}
        for sid, (at, before, after) in first_change.items()
        if at == first_ms
    ]
    return {
        "mode_id": mode_id,
        "initial": initial,
        "changes": changes,
        "duration_ms": duration_ms,
        "first_change_ms": first_ms,
        "causes_first_ms": causes,
        "source": src.name,
        "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "recorded_at": str(t0),
    }


def _write_mode(out_dir: Path, meta: Dict[str, Any], mode_id: str, data: Dict[str, Any]) -> None:
    header = {
        "mode_id": mode_id,
        "source": data["source"],
        "sha256": data["sha256"],
        "recorded_at_first": data["recorded_at"],
        "duration_ms": data["duration_ms"],
        "cause_at_ms": data["first_change_ms"],
        "stopped_at_ms": data["duration_ms"],
        "recovered_at_ms": data["duration_ms"],
        "cause_visible_after_stop": meta["fault"],
        "causes": data["causes_first_ms"],
        "initial": data["initial"],
    }
    out = out_dir / f"{mode_id}.gz"
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        for at_ms, sid, value in data["changes"]:
            encoded = "b1" if value is True else ("b0" if value is False else value)
            f.write(f"{at_ms}|{sid}|{encoded}\n")


def _resolve_source(src_dir: Path, filename: str) -> Path:
    """/tmp/<mode>.xlsx часто приходит с декомпозированным Unicode (й = и + ̆).
    Вместо побайтового сравнения сопоставляем по NFC-нормализованному имени."""
    want = unicodedata.normalize("NFC", filename)
    for path in _glob.glob(str(src_dir / "*.xlsx")):
        if unicodedata.normalize("NFC", path.rsplit("/", 1)[-1]) == want:
            return Path(path)
    return src_dir / filename


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Импорт записей режимов в recordings/")
    parser.add_argument("--src", default="/tmp/temp_repo", help="каталог с xlsx-файлами")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "backend/app/services/scenario/recordings"),
        help="каталог для сжатых данных режимов",
    )
    args = parser.parse_args()
    src_dir = Path(args.src)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: List[Dict[str, Any]] = []
    for filename, mode_id, title, fault, expected in MODES:
        src = _resolve_source(src_dir, filename)
        if not src.exists():
            print(f"ПРОПУЩЕН: {filename} (нет файла)")
            continue
        data = convert(src, mode_id)
        meta = {"id": mode_id, "title": title, "fault": fault, "expected_answer": expected}
        _write_mode(out_dir, meta, mode_id, data)
        manifest.append(
            {
                "mode": mode_id,
                "title": title,
                "fault": fault,
                "expected_answer": expected,
                "source": data["source"],
                "sha256": data["sha256"],
                "published_signals": len(data["initial"]),
                "changes": len(data["changes"]),
                "duration_ms": data["duration_ms"],
                "first_change_ms": data["first_change_ms"],
                "contains_dword_decode": False,
            }
        )
        print(
            f"OK {mode_id:16s} sig={len(data['initial']):2d} changes={len(data['changes']):6d} "
            f"dur={data['duration_ms']:6d}ms first={data['first_change_ms']:5d}ms src={data['source']}"
        )

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nМанифест: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())