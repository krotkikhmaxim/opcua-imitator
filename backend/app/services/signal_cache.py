"""Подтверждённое состояние сигналов (ТЗ #5, шаг 4 — §6).

У каждого сигнала карточка: значение, ревизия, источник последнего изменения
и время применения. Входа два:

- ``observe()``  — зеркало чтения (фид значений, REAL-сервер): значение
  освежается, ревизия не растёт — чтение не изменение;
- ``confirm()``  — подтверждённое применение (успешная запись в OPC UA или
  кэш имитатора): ревизия +1, источник и время фиксируются.

Ревизия монотонна в пределах жизни процесса и начинается с 0 (начальное
значение конфигурации ревизии не имеет). Персистентность состояния —
отдельный шаг ТЗ (§13), здесь её нет.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class SignalState:
    """Карточка подтверждённого состояния одного сигнала (§6)."""

    value: Any = None
    revision: int = 0
    source: Optional[str] = None
    changed_at: Optional[str] = None

    def card(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "revision": self.revision,
            "source": self.source,
            "changed_at": self.changed_at,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SignalCache:
    def __init__(self, signal_configs: List[Dict]):
        self._signals: Dict[str, Dict] = {}
        self._states: Dict[str, SignalState] = {}
        for cfg in signal_configs:
            self._signals[cfg["id"]] = cfg
            self._states[cfg["id"]] = SignalState(value=cfg.get("default"))

    @property
    def config(self) -> List[Dict]:
        return list(self._signals.values())

    def get_config(self, signal_id: str) -> Optional[Dict]:
        return self._signals.get(signal_id)

    def is_writable(self, signal_id: str) -> bool:
        cfg = self._signals.get(signal_id)
        return bool(cfg and cfg.get("writable", False))

    def get_value(self, signal_id: str) -> Optional[Any]:
        state = self._states.get(signal_id)
        return state.value if state is not None else None

    def get_state(self, signal_id: str) -> Optional[SignalState]:
        """Карточка сигнала (копия — вызывающий не мутирует состояние)."""
        state = self._states.get(signal_id)
        return SignalState(**state.__dict__) if state is not None else None

    def get_revision(self, signal_id: str) -> int:
        state = self._states.get(signal_id)
        return state.revision if state is not None else 0

    def observe(self, signal_id: str, value: Any) -> None:
        """Обновить зеркало значением чтения: без ревизии и источника."""
        state = self._states.get(signal_id)
        if state is not None:
            state.value = value

    def confirm(
        self,
        signal_id: str,
        value: Any,
        *,
        source: Optional[str] = None,
        changed_at: Optional[str] = None,
    ) -> int:
        """Зафиксировать применённое значение; вернуть новую ревизию."""
        state = self._states.setdefault(signal_id, SignalState())
        state.value = value
        state.revision += 1
        state.source = source
        state.changed_at = changed_at or _utc_now_iso()
        return state.revision

    def set_value(self, signal_id: str, value: Any) -> None:
        """Совместимость: подтверждение без имени источника."""
        self.confirm(signal_id, value)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "conf": list(self._signals.values()),
            "cache": {sid: state.value for sid, state in self._states.items()},
            "states": {sid: state.card() for sid, state in self._states.items()},
        }

    @staticmethod
    def normalize(value: Any) -> Any:
        if isinstance(value, bool):
            return bool(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return str(value)
