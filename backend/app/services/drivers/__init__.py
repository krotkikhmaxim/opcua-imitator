"""Драйверы значений: взаимное исключение источников записей и журнал правды.

Источник значений в имитаторе один — шина, но пишут в неё несколько сторон:
ручная запись оператора, сценарный движок и плейбэк записей. Реестр
(``registry.py``) следит, чтобы активным был ровно один драйвер, журнал
(``ledger.py``) копит все timestamped-записи, чтобы сверять состояние
имитатора с расчётом станции, шлюз (``gate.py``) на время активного драйвера
глушит heartbeat, который иначе перезаписывал бы SourceTimestamp и портил
шкалу для коллектора станции.
"""

from .gate import HeartbeatGate
from .ledger import WriteLedger
from .registry import DriverConflict, ValueDriverRegistry

__all__ = ["DriverConflict", "HeartbeatGate", "ValueDriverRegistry", "WriteLedger"]
