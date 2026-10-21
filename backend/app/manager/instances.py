"""Реестр дочерних экземпляров имитатора: один пользователь — один процесс.

Дочерний экземпляр — отдельный uvicorn-процесс с тем же ``app.main:app``
(роль single внутри): HTTP на ``127.0.0.1:<8101+i>``, OPC UA на
``0.0.0.0:<4841+i>``, всё состояние — в ``<users_root>/<user>/…``. Старт
дочернего процесса сам восстанавливает его личный снимок (§13.3), поэтому
«настройки вернулись после аутентификации» — свойство архитектуры, а не
отдельной процедуры. Останов — SIGTERM: штатный останов приложения
сохраняет последний снимок до закрытия шины.

Слоты устойчивы: распределение (порт, opc-порт, пара логин/пароль OPC UA)
хранится в файле реестра внутри корня пользователей, так что после рестарта
менеджера пользователь получает те же порты и те же учётные данные —
станция, подключённая по ``/internal/instances``, не перенастраивается.
Пароли в лог не попадают: на диск они идут только в файл реестра
(права 0600), в логах — ни имя, ни пароль.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

logger = logging.getLogger("opcua-imitator.manager")

#: Схема файла реестра менеджера (не путать с ответом /internal/instances).
REGISTRY_SCHEMA = "imitator.manager-registry.v1"

#: Имя файла реестра внутри корня пользователей.
REGISTRY_FILENAME = "manager_registry.json"

#: Задержка между опросами готовности дочернего процесса.
_POLL_INTERVAL_S = 0.1

#: Сколько секунд ждать штатного останова (SIGTERM) перед SIGKILL.
_STOP_GRACE_S = 8.0


class InstanceLimitError(Exception):
    """Ёмкость менеджера исчерпана (IMITATOR_MAX_INSTANCES)."""


class InstanceSpawnError(Exception):
    """Дочерний процесс не поднялся за отведённое время."""


@dataclass
class InstanceRecord:
    """Слот пользователя: назначение + живое состояние процесса."""

    user: str
    slot: int
    http_port: int
    opc_port: int
    username: str
    password: str
    created_at: str
    updated_at: str
    #: Хост, под которым станция видит OPC UA-порт (алиас контейнера).
    public_host: str = "opcua-imitator"
    # Только в памяти процесса менеджера: процесс, желание держать его
    # поднятым и отметка последней активности (для чистки простоя).
    process: Optional[subprocess.Popen] = None
    desired: str = "stopped"
    last_activity: float = field(default_factory=time.monotonic)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def state(self) -> str:
        if self.alive:
            return "running"
        if self.desired == "running":
            return "starting"
        return "stopped"

    def public(self) -> Dict:
        """Зафиксированный вид записи для /internal/instances (v1)."""
        return {
            "user": self.user,
            "http_port": self.http_port,
            "opc_port": self.opc_port,
            "state": self.state,
            "endpoint": f"opc.tcp://{self.public_host}:{self.opc_port}",
            "username": self.username,
            "password": self.password,
            "updated_at": self.updated_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InstanceRegistry:
    """Подъём, останов и чистка личных экземпляров; устойчивые слоты."""

    def __init__(
        self,
        users_root: Path,
        *,
        http_base_port: int = 8101,
        opc_base_port: int = 4841,
        max_instances: int = 8,
        idle_timeout_s: float = 1800.0,
        startup_timeout_s: float = 10.0,
        reaper_interval_s: float = 30.0,
        public_opc_host: str = "opcua-imitator",
        backend_dir: Optional[Path] = None,
        python_exe: Optional[str] = None,
        spawn_enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._users_root = Path(users_root)
        self._http_base_port = http_base_port
        self._opc_base_port = opc_base_port
        self._max_instances = max_instances
        self._idle_timeout_s = idle_timeout_s
        self._startup_timeout_s = startup_timeout_s
        self._reaper_interval_s = reaper_interval_s
        self._public_host = public_opc_host
        self._backend_dir = Path(backend_dir) if backend_dir else self._default_backend_dir()
        self._python_exe = python_exe or sys.executable
        self._spawn_enabled = spawn_enabled
        self._clock = clock
        self._records: Dict[str, InstanceRecord] = {}
        self._reaper_task: Optional[asyncio.Task] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._load_state()

    # -- служебное -----------------------------------------------------------

    @staticmethod
    def _default_backend_dir() -> Path:
        # .../app/manager/instances.py → .../backend
        return Path(__file__).resolve().parent.parent.parent

    @property
    def state_path(self) -> Path:
        return self._users_root / REGISTRY_FILENAME

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- файл реестра ---------------------------------------------------------

    def _load_state(self) -> None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("файл реестра экземпляров не читается: %s", self.state_path)
            return
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("файл реестра экземпляров повреждён: %s", self.state_path)
            return
        if not isinstance(document, dict) or document.get("schema") != REGISTRY_SCHEMA:
            logger.warning("файл реестра экземпляров другой схемы: %s", self.state_path)
            return
        for user, slot in (document.get("slots") or {}).items():
            try:
                self._records[user] = InstanceRecord(
                    user=user,
                    slot=int(slot["slot"]),
                    http_port=int(slot["http_port"]),
                    opc_port=int(slot["opc_port"]),
                    username=str(slot.get("username", "")),
                    password=str(slot.get("password", "")),
                    created_at=str(slot.get("created_at") or _now_iso()),
                    updated_at=str(slot.get("updated_at") or _now_iso()),
                    public_host=self._public_host,
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("слот пользователя %r в реестре негоден — пропущен", user)

    def _save_state(self) -> None:
        document = {
            "schema": REGISTRY_SCHEMA,
            "updated_at": _now_iso(),
            "slots": {
                user: {
                    "slot": record.slot,
                    "http_port": record.http_port,
                    "opc_port": record.opc_port,
                    "username": record.username,
                    "password": record.password,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                }
                for user, record in sorted(self._records.items())
            },
        }
        try:
            self._users_root.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
            # Пароли на диске — только в этом файле; доступ ограничиваем.
            try:
                os.chmod(self.state_path, 0o600)
            except OSError:
                pass
        except OSError:
            # Реестр на диске нужен для устойчивости слотов, но его сбой не
            # роняет запрос: пара портов переживёт и пересоздание.
            logger.exception("не удалось записать файл реестра экземпляров")

    # -- распределение слотов ---------------------------------------------------

    def _allocated_slots(self) -> set:
        return {record.slot for record in self._records.values()}

    def _allocate(self, user: str) -> InstanceRecord:
        if user in self._records:
            return self._records[user]
        used = self._allocated_slots()
        for slot in range(self._max_instances):
            if slot not in used:
                record = InstanceRecord(
                    user=user,
                    slot=slot,
                    http_port=self._http_base_port + slot,
                    opc_port=self._opc_base_port + slot,
                    username=f"imit_{user}",
                    password=secrets.token_urlsafe(16),
                    created_at=_now_iso(),
                    updated_at=_now_iso(),
                    public_host=self._public_host,
                )
                self._records[user] = record
                self._save_state()
                return record
        raise InstanceLimitError(
            f"достигнут предел экземпляров ({self._max_instances})"
        )

    def record_of(self, user: str) -> Optional[InstanceRecord]:
        return self._records.get(user)

    def records(self) -> List[InstanceRecord]:
        return list(self._records.values())

    # -- пути состояния дочернего процесса --------------------------------------

    def user_dir(self, user: str) -> Path:
        return self._users_root / user

    def child_env(self, record: InstanceRecord) -> Dict[str, str]:
        """Окружение дочернего процесса: всё состояние — под <root>/<user>."""
        root = self.user_dir(record.user)
        env = dict(os.environ)
        env.update(
            {
                # Дочерний процесс — обычный одиночный экземпляр; manager-режим
                # в нём не включается никогда.
                "IMITATOR_ROLE": "single",
                "OPC_UA_SERVER_ENDPOINT": f"opc.tcp://0.0.0.0:{record.opc_port}",
                "SAVED_STATES_DIR": str(root / "saved_states"),
                "SCENARIO_JOURNAL_PATH": str(root / "scenario_runs" / "journal.jsonl"),
                "VERIFICATION_DIR": str(root / "verifications"),
                "PERSIST_SNAPSHOT_PATH": str(root / "data" / "server_state.json"),
                "PLAYBACK_UPLOAD_DIR": str(root / "playback_uploads"),
                "OPC_UA_SERVER_USER": record.username,
                "OPC_UA_SERVER_PASSWORD": record.password,
            }
        )
        return env

    def child_cmd(self, record: InstanceRecord) -> List[str]:
        return [
            self._python_exe,
            "-m",
            "uvicorn",
            "--app-dir",
            str(self._backend_dir),
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(record.http_port),
            "--no-access-log",
        ]

    def _prepare_dirs(self, record: InstanceRecord) -> None:
        root = self.user_dir(record.user)
        for relative in (
            "saved_states",
            "scenario_runs",
            "verifications",
            "data",
            "playback_uploads",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)

    # -- подъём / останов ---------------------------------------------------------

    async def get_or_start(self, user: str) -> InstanceRecord:
        """Экземпляр пользователя; при необходимости — подъём (или 503)."""
        record = self._allocate(user)
        if record.alive:
            record.last_activity = self._clock()
            return record
        return await self._start(record)

    async def _start(self, record: InstanceRecord) -> InstanceRecord:
        # Перезапуск всегда с чистым описанием процесса: старый хэндл stale.
        record.process = None
        self._prepare_dirs(record)
        record.updated_at = _now_iso()
        if not self._spawn_enabled:
            self._save_state()
            return record
        process = subprocess.Popen(
            self.child_cmd(record),
            env=self.child_env(record),
            cwd=str(self._backend_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        record.process = process
        record.desired = "running"
        record.last_activity = self._clock()
        logger.info(
            "экземпляр %s: подъём (pid %d, http 127.0.0.1:%d, opc %d)",
            record.user,
            process.pid,
            record.http_port,
            record.opc_port,
        )
        try:
            await self._wait_ready(record)
        except Exception:
            await self._kill(process)
            record.process = None
            record.desired = "stopped"
            raise
        logger.info("экземпляр %s: готов на 127.0.0.1:%d", record.user, record.http_port)
        return record

    async def _wait_ready(self, record: InstanceRecord) -> None:
        """Опрос корня дочернего процесса; любой HTTP-ответ = сервер жив.

        (Без собранного фронтенда корень отвечает 404 — для готовности это
        тоже достаточный признак: приложение отвечает.)
        """
        url = f"http://127.0.0.1:{record.http_port}/"
        deadline = self._clock() + self._startup_timeout_s
        while self._clock() < deadline:
            if record.process is not None and record.process.poll() is not None:
                raise InstanceSpawnError(
                    f"экземпляр {record.user} завершился при подъёме "
                    f"(код {record.process.returncode})"
                )
            try:
                response = await self._client().get(url)
                await response.aclose()
                return
            except (httpx.HTTPError, OSError):
                await asyncio.sleep(_POLL_INTERVAL_S)
        raise InstanceSpawnError(
            f"экземпляр {record.user} не ответил за {self._startup_timeout_s:.0f} с"
        )

    @staticmethod
    async def _kill(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, _STOP_GRACE_S)
        except Exception:
            process.kill()
            try:
                await asyncio.to_thread(process.wait, 2.0)
            except Exception:
                pass

    async def stop(self, user: str, *, reason: str = "manual") -> bool:
        """SIGTERM: штатный останов дочернего процесса сохранит его снимок."""
        record = self._records.get(user)
        if record is None or not record.alive:
            return False
        process = record.process
        logger.info("экземпляр %s: останов (%s)", user, reason)
        record.desired = "stopped"
        record.process = None
        record.updated_at = _now_iso()
        self._save_state()
        await self._kill(process)
        return True

    async def stop_all(self) -> None:
        for user in list(self._records):
            await self.stop(user, reason="shutdown")

    # -- чистка простоя и перезапуск после падения --------------------------------

    def touch(self, user: str) -> None:
        record = self._records.get(user)
        if record is not None:
            record.last_activity = self._clock()

    def start_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop_reaper(self) -> None:
        task = self._reaper_task
        self._reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reaper_interval_s)
            try:
                await self.reap()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("чистка экземпляров упала")

    async def reap(self) -> None:
        """Один проход: простой — останов, падение — перезапуск."""
        now = self._clock()
        for record in list(self._records.values()):
            idle_for = now - record.last_activity
            if record.alive:
                if idle_for >= self._idle_timeout_s:
                    await self.stop(record.user, reason="idle")
                continue
            if record.desired != "running":
                continue
            # Падение: поднимаем заново, пока экземпляр не простоял сверх
            # лимита — тогда просто считаем его остановленным.
            if idle_for >= self._idle_timeout_s:
                record.desired = "stopped"
                continue
            logger.warning("экземпляр %s: падение, перезапуск", record.user)
            try:
                await self._start(record)
            except (InstanceSpawnError, InstanceLimitError, OSError):
                logger.exception("перезапуск экземпляра %s не удался", record.user)
                record.desired = "stopped"
