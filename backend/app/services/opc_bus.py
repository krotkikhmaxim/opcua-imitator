import asyncio
import os
from typing import Any, Dict, List, Optional

from app.services.signal_cache import SignalCache


class OPCMode:
    IMITATOR = "imitator"
    REAL = "real"


class OPCSignalBus:
    """
    Абстракция чтения/записи сигналов.

    В режиме IMITATOR используется встроенный кэш для генерации/чтения значений.
    В режиме REAL подключается к реальному OPC UA серверу через asyncua.
    """

    def __init__(self, cache: SignalCache, mode: str = OPCMode.IMITATOR,
                 endpoint: Optional[str] = None, timeout: int = 30):
        self.cache = cache
        self.mode = mode
        self.endpoint = endpoint or os.getenv("OPC_UA_ENDPOINT", "opc.tcp://localhost:4840")
        self.timeout = timeout
        self._client = None
        self._nodes: Dict[str, Any] = {}

    async def connect(self) -> None:
        if self.mode == OPCMode.REAL:
            try:
                from asyncua import Client
                self._client = Client(self.endpoint, timeout=self.timeout)
                await self._client.connect()
                ns = self._client.get_namespace_index(uri=os.getenv("OPC_UA_NAMESPACE", ""))
                for cfg in self.cache.config:
                    node_id = cfg.get("node_id")
                    if node_id:
                        self._nodes[cfg["id"]] = self._client.get_node(node_id)
            except Exception as e:
                raise RuntimeError(f"Ошибка подключения к OPC UA: {e}")

    async def read_value(self, signal_id: str) -> Optional[Any]:
        if self.mode == OPCMode.IMITATOR:
            return self.cache.get_value(signal_id)
        node = self._nodes.get(signal_id)
        if node is None:
            return None
        try:
            val = await node.read_value()
            self.cache.set_value(signal_id, val)
            return val
        except Exception:
            return None

    async def write_value(self, signal_id: str, value: Any) -> bool:
        if self.mode == OPCMode.IMITATOR:
            self.cache.set_value(signal_id, value)
            return True
        node = self._nodes.get(signal_id)
        if node is None:
            return False
        try:
            await node.set_value(value)
            self.cache.set_value(signal_id, value)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
