"""Сверка полного контура: журнал правды против снапшота станции.

Что проверяется и что — нет:

- **cause_check** — каждая причина эпизода ищется в точках снапшота
  (по ``binding_id``, запасной путь — по тегу) и сверяется значение.
  Причина с ``cause_visible_after_stop=false`` (estop_circuit_glitch)
  сверке не подлежит в принципе — она и должна быть видна только в
  истории, вердикт ``history_only``.
- **state_check** — ожидаемое состояние имитатора на момент остановки
  (``WriteLedger.expected_state_at`` поверх базиса исправной модели)
  против точек снапшота; присутствие точки учитывается отдельно от
  значения.
- **outputs** — только наблюдение за покрытием станции: сколько выходов
  рассчитано из объявленных и почему остальные не считаются. Вердикт не
  участвует: охват (~136 из 350) — свойство станции, а не ошибка стенда.

Итог: ``fail`` — хоть одна причина противоречит снапшоту; ``pass`` — все
причины сошлись (или ``history_only``) и расхождений состояния нет;
иначе ``partial`` — причины сошлись, но часть сигналов не привязана,
отсутствует или расходится с ожидаемым состоянием.

Модуль чистый: словари на входе, словарь на выходе — транспорт и файлы
живут в маршрутах, время и станция в тестах подменяются.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional

#: Ожидаемых расхождений состояния в отчёте не больше этого числа: отчёт —
#: не дамп снапшота, верхние N показывают характер, счётчик показывает всё.
MISMATCH_LIMIT = 50

CAUSE_VERDICTS = ("match", "mismatch", "not_bound", "absent", "history_only")


def values_equal(expected: Any, actual: Any) -> bool:
    """Сверка значений: булевы типы строго, числа с допуском на float."""
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, bool) != isinstance(actual, bool):
        return False  # bool против числа — расхождение типов, а не «примерно равно»
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-9)
    return expected == actual


def _point_index(points: List[Mapping[str, Any]]) -> tuple:
    by_binding = {p["binding_id"]: p for p in points if p.get("binding_id")}
    by_tag = {p["tag"]: p for p in points if p.get("tag")}
    return by_binding, by_tag


def cause_check(
    entry: Mapping[str, Any], by_binding: Mapping, by_tag: Mapping
) -> List[Dict[str, Any]]:
    visible = bool(entry.get("cause_visible_after_stop", True))
    verdicts: List[Dict[str, Any]] = []
    for cause in entry.get("causes") or []:
        point = by_binding.get(cause.get("signal_id")) or by_tag.get(cause.get("project_tag"))
        if not visible:
            verdict = "history_only"
        elif point is None:
            verdict = "not_bound"
        elif point.get("presence") != "present":
            verdict = "absent"
        elif values_equal(cause.get("after"), point.get("value")):
            verdict = "match"
        else:
            verdict = "mismatch"
        verdicts.append({
            "signal_id": cause.get("signal_id"),
            "name": cause.get("name"),
            "expected": cause.get("after"),
            "binding_id": point.get("binding_id") if point else None,
            "presence": point.get("presence") if point else None,
            "actual": point.get("value") if point else None,
            "source_timestamp": point.get("source_timestamp") if point else None,
            "verdict": verdict,
        })
    return verdicts


def state_check(
    expected_state: Mapping[str, Any], by_binding: Mapping
) -> Dict[str, Any]:
    present = matched = missing = 0
    mismatches: List[Dict[str, Any]] = []
    for signal_id, expected in sorted(expected_state.items()):
        point = by_binding.get(signal_id)
        if point is None or point.get("presence") != "present":
            missing += 1
            continue
        present += 1
        if values_equal(expected, point.get("value")):
            matched += 1
        elif len(mismatches) < MISMATCH_LIMIT:
            mismatches.append({
                "signal_id": signal_id,
                "expected": expected,
                "actual": point.get("value"),
                "source_timestamp": point.get("source_timestamp"),
            })
    mismatched = max(0, present - matched)
    return {
        "total": len(expected_state),
        "present": present,
        "missing": missing,
        "value_matched": matched,
        "value_mismatched": mismatched,
        "mismatches": mismatches,
    }


def outputs_observation(calculation: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Покрытие расчёта станции — как есть, без суждения о норме.

    Форма — ответ ``GET /api/telemetry/snapshots/{id}/results``:
    ``outputs_total``/``outputs_computed`` плюс ``composition``
    (``by_status`` и причины по группам). Отказ расчёта (``refusal``)
    проходит в наблюдение как есть — это факт станции, не ошибка сверки.
    """
    if not calculation:
        return None
    composition = calculation.get("composition") or {}
    by_status = composition.get("by_status") or {}
    total = calculation.get("outputs_total")
    if total is None:
        total = sum(by_status.values())
    computed = calculation.get("outputs_computed")
    if computed is None:
        computed = by_status.get("calculated", 0)
    reasons: Dict[str, Any] = {}
    for key, block in composition.items():
        if key == "by_status" or not isinstance(block, dict):
            continue
        for name, value in block.items():
            reasons[f"{key}.{name}"] = value
    observation: Dict[str, Any] = {
        "total": total,
        "computed": computed,
        "by_status": dict(sorted(by_status.items())),
        "reason_breakdown": reasons,
        "coverage": round(computed / total, 4) if total else None,
    }
    if calculation.get("refusal"):
        observation["refusal"] = calculation["refusal"]
    return observation


def build_report(
    entry: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    expected_state: Mapping[str, Any],
    calculation: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Полный отчёт сверки одного эпизода против одного снапшота."""
    points = snapshot.get("points") or []
    by_binding, by_tag = _point_index(points)
    verdicts = cause_check(entry, by_binding, by_tag)
    state = state_check(expected_state, by_binding)
    counts = Counter(v["verdict"] for v in verdicts)

    if counts.get("mismatch"):
        verdict = "fail"
    elif counts.get("not_bound") or counts.get("absent") or state["value_mismatched"]:
        verdict = "partial"
    else:
        verdict = "pass"

    return {
        "entry": {
            "run_id": entry.get("run_id"),
            "seed": entry.get("seed"),
            "episode": entry.get("episode"),
            "kind": entry.get("kind"),
            "title": entry.get("title"),
            "fault": entry.get("fault"),
            "cause_at": entry.get("cause_at"),
            "stopped_at": entry.get("stopped_at"),
        },
        "snapshot": {
            "id": snapshot.get("snapshot_id"),
            "project_id": snapshot.get("project_id"),
            "captured_at": snapshot.get("captured_at"),
            "capture_mode": snapshot.get("capture_mode"),
            "consistency": snapshot.get("consistency"),
            "value_spread_ms": snapshot.get("value_spread_ms"),
            "tags_total": snapshot.get("tags_total"),
            "tags_received": snapshot.get("tags_received"),
            "tags_missing": snapshot.get("tags_missing"),
            "tags_bad_quality": snapshot.get("tags_bad_quality"),
            "tags_untyped": snapshot.get("tags_untyped"),
        },
        "cause_check": {
            "verdicts": verdicts,
            "counts": {name: counts.get(name, 0) for name in CAUSE_VERDICTS},
        },
        "state_check": state,
        "outputs": outputs_observation(calculation),
        "verdict": verdict,
    }
