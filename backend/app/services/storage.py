import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings


class StateFileError(Exception):
    pass


class StateStorage:
    def __init__(self, directory: str = None):
        self.directory = Path(directory or settings.SAVED_STATES_DIR)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sanitize_name(name: str) -> str:
        name = (name or "").strip()
        name = re.sub(r"\s+", "_", name)
        name = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._-]", "", name)
        return name

    def generate_filename(self, name: str) -> str:
        safe = self.sanitize_name(name)
        if not safe:
            safe = "state"
        ts = datetime.now().strftime(settings.TIME_FORMAT)
        return f"{safe}_{ts}.json"

    def build_state(self, name: str, description: str,
                    signals: Dict[str, Any], signal_count: int) -> Dict[str, Any]:
        return {
            "name": name,
            "description": description or "",
            "created": datetime.now().isoformat(),
            "version": settings.FILE_VERSION,
            "signal_count": signal_count,
            "signals": signals,
        }

    def save(self, name: str, description: str,
             signals: Dict[str, Any], signal_count: int) -> str:
        filename = self.generate_filename(name)
        path = self.directory / filename
        payload = self.build_state(name, description, signals, signal_count)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.directory), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise StateFileError(f"Ошибка сохранения файла: {e}")
        return filename

    def resolve(self, filename: str) -> Path:
        filename = os.path.basename(filename)
        path = self.directory / filename
        if not path.exists():
            raise StateFileError(f"Файл не найден: {filename}")
        return path

    def read(self, filename: str) -> Dict[str, Any]:
        path = self.resolve(filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise StateFileError(f"Неверный JSON формат: {e}")
        except OSError as e:
            raise StateFileError(f"Ошибка чтения файла: {e}")
        return data

    def delete(self, filename: str) -> None:
        path = self.resolve(filename)
        try:
            path.unlink()
        except OSError as e:
            raise StateFileError(f"Ошибка удаления файла: {e}")

    def list_states(self) -> List[Dict[str, Any]]:
        result = []
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                result.append({
                    "filename": path.name,
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "created": data.get("created", ""),
                    "size": path.stat().st_size,
                    "signal_count": data.get("signal_count", 0),
                })
            except Exception:
                continue
        return result
