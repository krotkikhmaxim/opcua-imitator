"""Встроенный OPC UA сервер: сигналы конфигурации как переменные адресного пространства.

Сервер имитирует адресное пространство ПЛК: dotted-идентификаторы сигналов
(``Application.x10A3DigitalInput.SQ1_SinhLeftTop``) разворачиваются в иерархию
папок/переменных, а NodeId переменной равен полному IEC-пути сигнала, поэтому
внешний клиент может привязаться по ``ns=<idx>;s=<signal_id>``.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from asyncua import Server, ua

logger = logging.getLogger("opcua-imitator.server")

NAMESPACE_URI = "urn:opcua-imitator"
DEFAULT_ENDPOINT = "opc.tcp://0.0.0.0:4840"

VARIANT_TYPES = {
    # OPC UA spellings from the input-bindings file (the station maps these).
    "Boolean": ua.VariantType.Boolean,
    "Int16": ua.VariantType.Int16,
    "UInt16": ua.VariantType.UInt16,
    # Legacy spellings kept for old configs and the default fixture.
    "bool": ua.VariantType.Boolean,
    "int": ua.VariantType.Int32,
    "float": ua.VariantType.Double,
}


class EmbeddedOpcUaServer:
    """asyncua-сервер, публикующий сигналы ``signals.json`` как переменные."""

    def __init__(self, signal_configs: List[Dict], endpoint: str = DEFAULT_ENDPOINT):
        self._configs = signal_configs
        self._endpoint = endpoint
        self._server = Server()
        self._server.set_endpoint(endpoint)
        self._server.set_server_name("OPC UA Imitator (ПМ)")
        self._nodes: Dict[str, Any] = {}
        self._types: Dict[str, ua.VariantType] = {}
        self._started = False
        # The index of the imitator namespace, resolved at start(); the station
        # binds nodes as ``ns=2;s=...`` and this must match that index.
        self.namespace_index: int = 0

    @property
    def started(self) -> bool:
        return self._started

    @property
    def nodes(self) -> Dict[str, Any]:
        return self._nodes

    async def start(self) -> None:
        if self._started:
            return
        await self._server.init()
        idx = await self._server.register_namespace(NAMESPACE_URI)
        self.namespace_index = idx
        logger.info("imitator namespace %r registered at index %d", NAMESPACE_URI, idx)
        objects = self._server.nodes.objects
        folders: Dict[str, Any] = {"": objects}
        for cfg in self._configs:
            signal_id = cfg["id"]
            # The published NodeId is the full IEC path (what the station's
            # bindings reference); the config key stays the readable short id.
            node_id = cfg.get("node_id") or signal_id
            parts = node_id.split(".")
            parent = objects
            prefix: List[str] = []
            for part in parts[:-1]:
                prefix.append(part)
                key = ".".join(prefix)
                if key not in folders:
                    folders[key] = await parent.add_folder(
                        ua.NodeId(key, idx),
                        ua.QualifiedName(part, idx),
                    )
                parent = folders[key]
            vtype = VARIANT_TYPES.get(cfg.get("type", ""), ua.VariantType.String)
            node = await parent.add_variable(
                ua.NodeId(node_id, idx),
                ua.QualifiedName(parts[-1], idx),
                cfg.get("default"),
                vtype,
            )
            # External clients see the inputs as read-only (opcua_writable, per
            # the source file); the imitator's own simulation writes bypass this
            # flag by writing through the internal node handle. Legacy configs
            # without opcua_writable keep using their writable flag.
            await node.set_writable(bool(cfg.get("opcua_writable", cfg.get("writable", False))))
            self._nodes[signal_id] = node
            self._types[signal_id] = vtype

        await self._server.start()
        self._started = True
        logger.info(
            "embedded OPC UA server listening on %s (%d signals)",
            self._endpoint,
            len(self._nodes),
        )

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await self._server.stop()
        finally:
            self._started = False

    async def read_value(self, signal_id: str) -> Optional[Any]:
        node = self._nodes.get(signal_id)
        if node is None:
            return None
        try:
            return await node.read_value()
        except Exception:
            logger.warning("failed to read %s", signal_id, exc_info=True)
            return None

    async def write_value(self, signal_id: str, value: Any) -> bool:
        node = self._nodes.get(signal_id)
        if node is None:
            return False
        try:
            await node.write_value(value, varianttype=self._types.get(signal_id))
            return True
        except Exception:
            logger.warning("failed to write %s", signal_id, exc_info=True)
            return False

    async def touch(self, signal_id: str, when: Optional[datetime] = None) -> bool:
        """Re-publish the current value with a fresh SourceTimestamp.

        Heartbeat (cyclic refresh): переписывает текущее значение переменной в
        адресное пространство, сохраняя ``Value`` и OPC UA тип, но присваивая
        новый ``SourceTimestamp`` (UTC) и ``StatusCode=Good`` — даже если само
        значение не изменилось. Имитирует циклическое подтверждение источника.
        """
        node = self._nodes.get(signal_id)
        if node is None:
            return False
        try:
            current = await node.read_data_value()
            vtype = current.Value.VariantType
            if vtype in (None, ua.VariantType.Null):
                vtype = self._types.get(signal_id)
            dv = ua.DataValue(
                ua.Variant(current.Value.Value, vtype),
                ua.StatusCode(ua.StatusCodes.Good),
                when or datetime.now(timezone.utc),
            )
            await node.write_value(dv)
            return True
        except Exception:
            logger.warning("failed to touch %s", signal_id, exc_info=True)
            return False