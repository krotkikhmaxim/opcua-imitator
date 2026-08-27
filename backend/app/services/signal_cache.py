from typing import Any, Dict, List, Optional


class SignalCache:
    def __init__(self, signal_configs: List[Dict]):
        self._signals: Dict[str, Dict] = {}
        self._cache: Dict[str, Optional[Any]] = {}
        for cfg in signal_configs:
            self._signals[cfg["id"]] = cfg
            self._cache[cfg["id"]] = cfg.get("default")

    @property
    def config(self) -> List[Dict]:
        return list(self._signals.values())

    def get_config(self, signal_id: str) -> Optional[Dict]:
        return self._signals.get(signal_id)

    def is_writable(self, signal_id: str) -> bool:
        cfg = self._signals.get(signal_id)
        return bool(cfg and cfg.get("writable", False))

    def get_value(self, signal_id: str) -> Optional[Any]:
        return self._cache.get(signal_id)

    def set_value(self, signal_id: str, value: Any) -> None:
        self._cache[signal_id] = value

    def snapshot(self) -> Dict[str, Any]:
        return {
            "conf": list(self._signals.values()),
            "cache": dict(self._cache),
        }

    @staticmethod
    def normalize(value: Any) -> Any:
        if isinstance(value, bool):
            return bool(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return str(value)
