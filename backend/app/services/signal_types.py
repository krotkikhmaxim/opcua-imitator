"""Единая таблица типов сигналов (ТЗ #5, шаг 3 — §5 и §11).

До этого шага приведение значений жило в четырёх местах с разными правилами
(``main._coerce_value``, ``playback/xlsx._coerce``, ``archive_reader._coerce_value``,
``embedded_server.VARIANT_TYPES``): Double не существовал, любое число молча
становилось Boolean, дробное усекалось до целого, неизвестный тип в сервере
молча становился строкой. Здесь одна таблица: резолв типа, значение по
умолчанию, приведение с диагностикой и проверка конфигурации реестра.

Контракты входа (§11: «0/1 → Boolean только по явному контракту входа»):

- ``contract="api"``  — ручной запрос через REST: число к Boolean не приводится;
- ``contract="file"`` — объявленный файловый вход (строка конфигурации, колонка
  тренда, ячейка архива, сценарий): целые 0/1 и «0.0/1.0» к Boolean допускаются.

Правила, одинаковые для обоих контрактов: целые типы не принимают дробные и
логические значения и проверяют диапазон; Double не усекается и не принимает
NaN/бесконечность; ошибки — исключение с именем сигнала, никогда не нулевое
значение.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional


class SignalValueError(ValueError):
    """Значение не приводится к типу сигнала (текст — диагностика для клиента)."""


class SignalConfigError(RuntimeError):
    """Конфигурация реестра противоречива (ТЗ §5: не чинить молча — падать)."""


@dataclass(frozen=True)
class SignalType:
    """Один тип из сквозного набора §11 плюс наследимые написания."""

    name: str  # каноническое имя (то, что несёт конфигурация)
    kind: str  # "bool" | "int" | "float"
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    default: Any = None


# Сквозной набор §11. Double добавлен шагом 3 — до него аналоговые значения
# усекались до целого.
_BOOLEAN = SignalType("Boolean", "bool", default=False)
_INT16 = SignalType("Int16", "int", -32768, 32767, 0)
_UINT16 = SignalType("UInt16", "int", 0, 65535, 0)
_INT32 = SignalType("Int32", "int", -(2**31), 2**31 - 1, 0)  # наследие: "int"/"float"-конфиги
_DOUBLE = SignalType("Double", "float", default=0.0)

_TYPES: dict[str, SignalType] = {
    "Boolean": _BOOLEAN,
    "Int16": _INT16,
    "UInt16": _UINT16,
    "Int32": _INT32,
    "Double": _DOUBLE,
    # Наследуемые написания: 10 фикстурных конфигов (config.py) пишут
    # bool/int/float, колонки архива станции — boolean/integer/long/double.
    "bool": _BOOLEAN,
    "boolean": _BOOLEAN,
    "int": _INT32,
    "integer": _INT32,
    "long": _INT32,
    "float": _DOUBLE,
    "double": _DOUBLE,
}

# Для серверных вариантов и доков: только канонические имена.
CANONICAL_TYPES: tuple[str, ...] = ("Boolean", "Int16", "UInt16", "Double")


def resolve_type(name: Any) -> SignalType:
    """Тип по написанию из конфигурации; неизвестное — ошибка конфигурации."""
    try:
        return _TYPES[str(name)]
    except (KeyError, TypeError):
        known = ", ".join(CANONICAL_TYPES)
        raise SignalConfigError(
            f"неизвестный тип сигнала {name!r}: поддерживаются {known} "
            "(и наследуемые bool/int/float)"
        ) from None


def default_for(name: Any) -> Any:
    """Значение по умолчанию типа (Boolean→False, Int16/UInt16→0, Double→0.0)."""
    return resolve_type(name).default


def _range_of(st: SignalType) -> str:
    return f"[{st.min_value}; {st.max_value}]"


def coerce(signal_type: Any, value: Any, *, contract: str = "api") -> Any:
    """Значение к типу сигнала; не приводится — SignalValueError, никогда не «0».

    ``None`` — не значение (пустую ячейку файла вызывающий отфильтровал сам):
    и здесь это отклонение, а не False/0.
    """
    if value is None:
        raise SignalValueError("значение отсутствует (null)")
    st = resolve_type(signal_type)
    if contract not in ("api", "file"):
        raise SignalValueError(f"неизвестный контракт входа {contract!r}")

    if isinstance(value, bool):
        if st.kind == "bool":
            return value
        raise SignalValueError(f"логическое значение не приводится к {st.name}")

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise SignalValueError("пустая строка не является значением")
        low = text.lower()
        if st.kind == "bool":
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
            raise SignalValueError(
                f"строка {value!r} не читается как Boolean "
                "(true/false/1/0/yes/no/on/off)"
            )
        if st.kind == "int":
            try:
                parsed = int(text, 10)
            except ValueError:
                raise SignalValueError(
                    f"строка {value!r} не читается как целое ({st.name})"
                ) from None
            return _check_int(st, parsed)
        # Double из строки: float("nan"/"inf") парсится — отсечётся проверкой ниже.
        try:
            parsed = float(text)
        except ValueError:
            raise SignalValueError(f"строка {value!r} не читается как Double") from None
        return _check_double(parsed)

    if isinstance(value, int):
        if st.kind == "bool":
            if contract == "file" and value in (0, 1):
                return bool(value)
            raise SignalValueError(
                f"число {value} не приводится к Boolean по контракту {contract!r}"
            )
        if st.kind == "int":
            return _check_int(st, value)
        return _check_double(float(value))  # целое в Double — расширение без потерь

    if isinstance(value, float):
        if st.kind == "bool":
            if contract == "file" and value in (0.0, 1.0):
                return bool(value)
            raise SignalValueError(f"число {value} не приводится к Boolean")
        if st.kind == "int":
            if not math.isfinite(value):
                raise SignalValueError(f"{value} не является допустимым числом")
            if not value.is_integer():
                raise SignalValueError(
                    f"дробное {value} не приводится к {st.name} без усечения"
                )
            return _check_int(st, int(value))
        return _check_double(value)

    raise SignalValueError(
        f"значение типа {type(value).__name__} не приводится к {st.name}"
    )


def _check_int(st: SignalType, value: int) -> int:
    if st.min_value is not None and value < st.min_value:
        raise SignalValueError(f"{value} вне диапазона {st.name} {_range_of(st)}")
    if st.max_value is not None and value > st.max_value:
        raise SignalValueError(f"{value} вне диапазона {st.name} {_range_of(st)}")
    return value


def _check_double(value: float) -> float:
    if not math.isfinite(value):
        # §11: по умолчанию NaN и бесконечность не принимаются.
        raise SignalValueError(f"{value!r} не является допустимым числом Double")
    return float(value)


def node_local_id(cfg: dict) -> str:
    """Эффективный локальный идентификатор узла: ``node_id``, иначе сам id.

    Так же его вычисляет embedded_server при публикации; расхождений быть не
    должно (asyncua на дубле NodeId падает молча — дубль ловит validate_configs).
    """
    local = str((cfg or {}).get("node_id", "")).strip()
    if not local:
        local = str((cfg or {}).get("id", "")).strip()
    if not local:
        raise SignalConfigError("у сигнала нет ни id, ни node_id")
    return local


def validate_configs(configs: Iterable[dict], *, source: str) -> None:
    """Проверка всего реестра до публикации (ТЗ §5).

    Собирает ВСЕ проблемы, а не первую: дубли id, дули эффективного NodeId,
    неизвестные типы, начальные значения вне типа. Любая проблема — ошибка
    конфигурации с именем источника; неоднозначности не исправляются молча.
    """
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_nodes: dict[str, str] = {}
    for index, cfg in enumerate(configs, start=1):
        sid = str((cfg or {}).get("id", "")).strip()
        where = f"{source}[{index}]"
        if not sid:
            errors.append(f"{where}: у сигнала пустой id")
            continue
        where = f"{source}[{index}] {sid}"
        if sid in seen_ids:
            errors.append(f"{where}: id дублирует {seen_ids[sid]}")
        else:
            seen_ids[sid] = where
        try:
            local = node_local_id(cfg)
        except SignalConfigError as exc:
            errors.append(f"{where}: {exc}")
            local = ""
        if local:
            if local in seen_nodes:
                errors.append(f"{where}: NodeId дублирует {seen_nodes[local]}")
            else:
                seen_nodes[local] = where
        type_name = (cfg or {}).get("type")
        try:
            st = resolve_type(type_name)
        except SignalConfigError as exc:
            errors.append(f"{where}: {exc}")
            continue
        initial = (cfg or {}).get("default", st.default)
        try:
            coerce(st.name, initial, contract="file")
        except SignalValueError as exc:
            errors.append(f"{where}: начальное значение {initial!r}: {exc}")
    if errors:
        listed = "\n".join(f"  - {line}" for line in errors)
        raise SignalConfigError(
            f"конфигурация сигналов {source} противоречива ({len(errors)}):\n{listed}"
        )
