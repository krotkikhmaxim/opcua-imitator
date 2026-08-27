import os
from typing import List, Dict


class Settings:
    SAVED_STATES_DIR: str = os.getenv("SAVED_STATES_DIR", "saved_states")
    SIGNAL_CONFIG_PATH: str = os.getenv("SIGNAL_CONFIG_PATH", "configs/signals.json")
    TIME_FORMAT: str = "%Y%m%d_%H%M%S"
    FILE_VERSION: str = "1.0"
    MAX_FILE_SIZE: int = 10 * 1024 * 1024
    MAX_SIGNAL_COUNT: int = 1000


settings = Settings()


def load_signal_config(path: str) -> List[Dict]:
    import json

    default = [
        {"id": "signal_1", "name": "Скорость подъёма", "type": "float", "writable": True, "default": 3.5},
        {"id": "signal_2", "name": "Температура двигателя", "type": "float", "writable": False, "default": 78.2},
        {"id": "signal_3", "name": "Масса груза", "type": "float", "writable": True, "default": 1250.0},
        {"id": "signal_4", "name": "Уровень масла", "type": "float", "writable": True, "default": 64.4},
        {"id": "signal_5", "name": "Режим работы", "type": "int", "writable": True, "default": 1},
        {"id": "signal_6", "name": "Аварийный стоп", "type": "bool", "writable": True, "default": False},
        {"id": "signal_7", "name": "Датчик перегруза", "type": "bool", "writable": False, "default": False},
        {"id": "signal_8", "name": "Заданная скорость", "type": "float", "writable": True, "default": 2.0},
        {"id": "signal_9", "name": "Положение клети", "type": "int", "writable": False, "default": 120},
        {"id": "signal_10", "name": "Состояние тормоза", "type": "bool", "writable": True, "default": True},
    ]

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            return loaded
        except Exception:
            return default
    return default
