"""Снимок подтверждённого состояния сервера (ТЗ #5, шаг 8 — §13).

Один файл, атомарная замена (tmp + os.replace — порванного снимка не
бывает): схема, отпечаток реестра, карточки сигналов {v, r, s, t} с
ревизиями и источниками, время создания. Позиция таймлайна и тренд не
сохраняются (§13.1).

Когда сохраняется: после успешных команд применения, на переходах
сценария (пауза/остановка/завершение/ошибка), периодически во время
прогона (интервал настраивается, по умолчанию 5 с) и при штатном
останове сервера. Никакой записи на каждый шаг сценария: высокочастотные
смены идут через писца движка, снимок — только на перечисленных
границах, то есть всегда на завершённой границе применения, не внутри
пакета (§13.2).

Восстановление (§13.3): снимок проверяется целиком — схема, отпечаток
реестра, целостность, корректность значений; повреждённый или
несовместимый снимок отбрасывается ВЕСЬ, сервер стартует с начальными
значениями и явной диагностикой (никакой частичной неявной миграции).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("opcua-imitator.persistence")

#: Версия схемы снимка; несовместимая схема = снимок целиком в отказ.
SNAPSHOT_SCHEMA = "opcua-imitator.server-snapshot.v1"


def registry_fingerprint(configs: List[Dict]) -> str:
    """Отпечаток реестра: id + тип + адрес каждого сигнала, порядок фиксир."""
    material = [
        (cfg["id"], cfg.get("type", ""), cfg.get("opcua_node") or cfg.get("address") or "")
        for cfg in sorted(configs, key=lambda c: c["id"])
    ]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()[:12]


class SnapshotStore:
    """Сохранение и загрузка снимка подтверждённого состояния (§13)."""

    def __init__(
        self,
        path: Path,
        fingerprint: str,
        signal_ids: List[str],
        read_card: Callable[[str], Dict[str, Any]],
        interval_s: float = 5.0,
        active: Callable[[], bool] = lambda: False,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._path = path
        self._fingerprint = fingerprint
        self._signal_ids = signal_ids
        self._read_card = read_card
        self._interval_s = interval_s
        self._active = active
        self._now = now
        self._task: Optional[asyncio.Task] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def periodic_disabled(self) -> bool:
        """Периодический снимок выключен конфигурацией (интервал 0)."""
        return self._interval_s <= 0

    # -- сохранение ---------------------------------------------------------

    def build_document(self) -> Dict[str, Any]:
        """Снимок по текущему состоянию; вызывается на границе применения."""
        signals: Dict[str, Any] = {}
        for signal_id in self._signal_ids:
            card = self._read_card(signal_id)
            signals[signal_id] = {
                "v": card.get("value"),
                "r": card.get("revision", 0),
                "s": card.get("source"),
                "t": card.get("changed_at"),
            }
        return {
            "schema": SNAPSHOT_SCHEMA,
            "registry": self._fingerprint,
            "created_at": self._now().isoformat(),
            "signals": signals,
            "complete": True,
        }

    def save(self) -> bool:
        """Атомарная запись снимка; сбой логируется, но не роняет клиента."""
        document = self.build_document()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
            return True
        except Exception:
            logger.exception("не удалось записать снимок состояния в %s", self._path)
            return False

    # -- загрузка -----------------------------------------------------------

    def load(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """Прочитать и проверить снимок целиком; (None, причина) при отказе."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, "снимка нет (первый запуск или снимок удалён)"
        except OSError as exc:
            return None, f"снимок не читается: {exc}"
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"снимок повреждён (не JSON): {exc}"
        if not isinstance(document, dict):
            return None, "снимок повреждён (не объект)"
        if document.get("schema") != SNAPSHOT_SCHEMA:
            return None, f"несовместимая схема снимка: {document.get('schema')!r}"
        if document.get("registry") != self._fingerprint:
            return None, (
                f"снимок другого реестра ({document.get('registry')!r} "
                f"против текущего {self._fingerprint!r})"
            )
        if document.get("complete") is not True or not isinstance(document.get("signals"), dict):
            return None, "снимок неполный (нет отметки завершённости или карточек)"
        return document, "ok"

    # -- периодический снимок -------------------------------------------------

    def start(self) -> None:
        if self.periodic_disabled or self.running:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                # Периодический снимок — только пока идёт сценарий (§13.2);
                # спокойное состояние сохраняют команды и останов сервера.
                if self._active():
                    self.save()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("periodic snapshot failed")
