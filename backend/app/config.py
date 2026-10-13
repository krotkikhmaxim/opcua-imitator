import os
from pathlib import Path
from typing import Dict, List, Optional


class Settings:
    SAVED_STATES_DIR: str = os.getenv("SAVED_STATES_DIR", "saved_states")
    # The input-bindings file in ../docs is the single source of the published
    # tag set (schema telemetry.opcua.input-bindings.v1). Resolved against this
    # file so the path works whatever the working directory of uvicorn is.
    SIGNAL_CONFIG_PATH: str = os.getenv(
        "SIGNAL_CONFIG_PATH",
        str(Path(__file__).resolve().parent.parent.parent / "docs" / "opcua_input_bindings.json"),
    )
    # Журнал правды сценариев (JSONL): что случилось и какой ответ эталонный.
    SCENARIO_JOURNAL_PATH: str = os.getenv("SCENARIO_JOURNAL_PATH", "scenario_runs/journal.jsonl")
    # Отчёты сверки с журналом правды (шаг 6): по файлу на сверку.
    VERIFICATION_DIR: str = os.getenv("VERIFICATION_DIR", "scenario_runs/verifications")
    # WS-фид дельт значений: интервал рассылки в секундах; 0 отключает фид.
    VALUES_FEED_INTERVAL_S: float = float(os.getenv("VALUES_FEED_INTERVAL_S", "0.5"))
    # Плейбэк XLSX-трендов: лимит размера файла (экспорт владельца — сотни
    # мегабайт) и лимит прямой загрузки через HTTP (большие файлы — по пути
    # сервер-сайд, PLAYBACK_UPLOAD_DIR).
    PLAYBACK_MAX_BYTES: int = int(os.getenv("PLAYBACK_MAX_BYTES", 1024 * 1024 * 1024))
    PLAYBACK_UPLOAD_MAX_BYTES: int = int(os.getenv("PLAYBACK_UPLOAD_MAX_BYTES", 50 * 1024 * 1024))
    PLAYBACK_UPLOAD_DIR: str = os.getenv("PLAYBACK_UPLOAD_DIR", "playback_uploads")
    # Плейбэк архива станции: путь к SQLite (read-only) и лимит отсчётов окна
    # — окно сверх лимита отклоняется, а не усекается.
    ARCHIVE_PATH: str = os.getenv(
        "ARCHIVE_PATH",
        str(Path("/home/max/projects/asutpv1_bigllm/data/telemetry/archive.sqlite3")),
    )
    PLAYBACK_MAX_CHANGES: int = int(os.getenv("PLAYBACK_MAX_CHANGES", "200000"))
    # Станция asutpv1_bigllm: база прокси, таймаут и проект по умолчанию
    # для снимков live_batch (панель «эхо» и сверка).
    STATION_URL: str = os.getenv("STATION_URL", "http://127.0.0.1:8080")
    STATION_TIMEOUT_S: float = float(os.getenv("STATION_TIMEOUT_S", "5"))
    STATION_PROJECT_ID: str = os.getenv("STATION_PROJECT_ID", "124_NSH2_PS")
    # Мост «обозначение Э3 → сигналы стенда» (stand.schematic-bridge.v1): его
    # строит scripts/build_schematic_bridge.py по каталогу листов станции.
    SCHEMATIC_BRIDGE_PATH: str = os.getenv(
        "SCHEMATIC_BRIDGE_PATH",
        str(Path(__file__).resolve().parent.parent / "configs" / "schematic_bridge.json"),
    )
    TIME_FORMAT: str = "%Y%m%d_%H%M%S"
    FILE_VERSION: str = "1.0"
    MAX_FILE_SIZE: int = 10 * 1024 * 1024
    MAX_SIGNAL_COUNT: int = 1000


settings = Settings()

# OPC UA type names from the input-bindings file. The station's telemetry maps
# these same names (Boolean/Int16/UInt16) to canonical types, so the imitator
# keeps the OPC UA spelling verbatim instead of the legacy bool/int/float.
DEFAULT_BY_TYPE = {
    "Boolean": False,
    "Int16": 0,
    "UInt16": 0,
}


def _default_signal_configs() -> List[Dict]:
    return [
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


def _parse_input_bindings(payload: Dict) -> List[Dict]:
    """One row of ``telemetry.opcua.input-bindings.v1`` → a signal config.

    ``id`` stays the readable short key (UI/cache). ``node_id`` is the full IEC
    path the embedded OPC UA server publishes (the ``ns=2;s=...`` string minus
    its scheme) — that is exactly the string the station binds against. The
    imitator may write every input to simulate values (``writable``), while the
    OPC UA variable itself stays read-only to external clients
    (``opcua_writable``, per the source file).
    """
    configs: List[Dict] = []
    for item in payload.get("bindings", []):
        node_id = item.get("opcua_node", "")
        if node_id.startswith("ns="):
            node_id = node_id.split(";", 1)[-1]
            if node_id.startswith("s="):
                node_id = node_id[2:]
        if not node_id:
            node_id = item.get("project_tag", item.get("id", ""))
        signal_type = item.get("type", "Boolean")
        configs.append({
            "id": item.get("id", node_id),
            "name": item.get("name", item.get("id", node_id)),
            "type": signal_type,
            "writable": True,
            "opcua_writable": bool(item.get("writable", False)),
            "default": DEFAULT_BY_TYPE.get(signal_type, False),
            "node_id": node_id,
            "address": node_id,
            "project_tag": item.get("project_tag", node_id),
            "channel": item.get("channel"),
            "cabinet": item.get("cabinet"),
            "module": item.get("module"),
            "direction": item.get("direction"),
            "bit": item.get("bit"),
            "device": item.get("device"),
            "word_address": item.get("word_address"),
            "logic": item.get("logic"),
        })
    return configs


def load_signal_config(path: Optional[str] = None) -> List[Dict]:
    """Read a signal config file, detecting its shape.

    - ``telemetry.opcua.input-bindings.v1`` (a dict with ``bindings``) — the
      operator-facing tag list under ``docs/``;
    - a plain JSON list — the legacy ``configs/signals.json`` shape;
    - anything else or a missing/unreadable file — the built-in defaults.
    """
    import json

    path = path or settings.SIGNAL_CONFIG_PATH
    default = _default_signal_configs()
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        return default
    if isinstance(loaded, dict):
        if loaded.get("schema") == "telemetry.opcua.input-bindings.v1" or "bindings" in loaded:
            return _parse_input_bindings(loaded)
        items = loaded.get("items")
        if isinstance(items, list):
            return items
        return default
    if isinstance(loaded, list):
        return loaded
    return default