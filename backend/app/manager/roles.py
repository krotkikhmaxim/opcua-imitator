"""Роли и шлюз доступа (единая точка разбора edge-заголовков).

Аутентификацию делает edge (Caddy + Authelia): в запросе приходят заголовки
``Remote-User`` и ``Remote-Groups``. Менеджер им не доверяет слепо — при
заданном ``IMITATOR_EDGE_TOKEN`` заголовки принимаются только вместе с
``X-Edge-Token``, равным ему (сравнение в постоянном времени); без аттестации
и вовсе без заголовков запрос разбирается как оператор — читать можно,
изменять нельзя. Отсюда один и тот же шлюз закрывает и прокси к личным
экземплярам, и маршруты дефолтного экземпляра в процессе менеджера: оператор
не проходит записью ни своим экземпляром, ни «админской полосой».
"""

from __future__ import annotations

import hmac
import os
from enum import Enum
from typing import Mapping, Optional, Tuple

# Нормализация пользователя повторяет станцию: casefold, обрезка служебных
# символов заголовка, потолок 64. Неизвестно/пусто → пользователь не выделен.
_FORBIDDEN_CHARS = "\"'\\/,;<>[]{}|"
_MAX_USER_LEN = 64


class Role(str, Enum):
    ADMIN = "admin"
    ENGINEER = "engineer"
    OPERATOR = "operator"


def normalize_user(raw: str) -> str:
    """Заголовок Remote-User → безопасное имя экземпляра (правила станции).

    На станции имя — ключ записи, здесь оно становится компонентой пути
    личного состояния (``<users_root>/<user>/…``), поэтому имя, которое после
    чистки — «.» или «..», не имя вовсе: пустой результат, пользователь не
    выделен, личный экземпляр не поднимается (выход из корня пользователей
    обрезан на нормализации, а не на доверии к краю).
    """
    cleaned = raw.strip()
    for ch in _FORBIDDEN_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip().casefold()[:_MAX_USER_LEN]
    return "" if cleaned in (".", "..") else cleaned


def parse_groups(raw: str) -> set:
    """Remote-Groups: разделители запятая/точка с запятой, регистр не важен."""
    groups = set()
    for chunk in raw.replace(";", ",").split(","):
        name = chunk.strip().casefold()
        if name:
            groups.add(name)
    return groups


def _header(headers: Mapping, name: str) -> str:
    """Чтение заголовка без оглядки на регистр (Starlette — сам, dict — сканом)."""
    got = headers.get(name)
    if got is not None:
        return got
    low = name.lower()
    for key, value in headers.items():
        if str(key).lower() == low:
            return value
    return ""


def trusted_headers(headers: Mapping) -> bool:
    """Аттестация edge-заголовков; без IMITATOR_EDGE_TOKEN доверяем как есть."""
    expected = os.getenv("IMITATOR_EDGE_TOKEN", "")
    if not expected:
        return True
    got = _header(headers, "x-edge-token")
    return hmac.compare_digest(got.encode(), expected.encode())


def identity_of(headers: Mapping) -> Optional[str]:
    """Пользователь запроса или None (нет/не проходит аттестацию/пустой)."""
    if not trusted_headers(headers):
        return None
    user = normalize_user(_header(headers, "remote-user"))
    return user or None


def role_of(headers: Mapping) -> Role:
    """Роль по группам; неизвестное/отсутствующее — всегда оператор."""
    if not trusted_headers(headers):
        return Role.OPERATOR
    groups = parse_groups(_header(headers, "remote-groups"))
    if "admins" in groups:
        return Role.ADMIN
    if "engineers" in groups:
        return Role.ENGINEER
    return Role.OPERATOR


# -- матрица прав: что оператору запрещено ----------------------------------
#
# Список зафиксирован (WP-F): изменяющие эндпоинты — только инженерам и
# админам; читающие открыты всем, включая операторов.

_MUTATING_PREFIXES = (
    "/api/states",
    "/api/scenarios",
    "/api/machine",
    "/api/playback",
)

_MUTATING_SIGNAL_PATHS = (
    "/api/signals/write",
    "/api/signals/write_batch",
    "/api/signals/reset",
)

_MUTATING_HEARTBEAT_METHODS = ("PUT", "POST", "DELETE", "PATCH")


def is_mutating(method: str, path: str) -> bool:
    """Изменяющий ли запрос по зафиксированной матрице (WP-F)."""
    method = (method or "GET").upper()
    if path in _MUTATING_SIGNAL_PATHS:
        return method != "GET"
    if any(path == p or path.startswith(p + "/") for p in _MUTATING_PREFIXES):
        return method != "GET"
    if path == "/api/heartbeat":
        return method in _MUTATING_HEARTBEAT_METHODS
    return False


def gate_rejection(role: Role, method: str, path: str) -> bool:
    """True — шлюз отказывает: оператор и изменяющий эндпоинт."""
    return role is Role.OPERATOR and is_mutating(method, path)


def resolve(headers: Mapping) -> Tuple[Optional[str], Role]:
    """Одна точка разбора: (пользователь | None, роль)."""
    return identity_of(headers), role_of(headers)
