#!/usr/bin/env python3
"""Дымовой тест OPC UA-сервера имитатора.

Проверяет не «порт открыт», а что клиент реально читает узлы имитатора:
  1) подключение по endpoint;
  2) наличие пространства имён urn:opcua-imitator;
  3) обход дерева и подсчёт переменных в этом пространстве имён;
  4) чтение значений выборки переменных;
  5) (опционально) что внешняя запись отклоняется — станция read-only, Режим B.

Примеры:
  python scripts/smoke_opcua.py --endpoint opc.tcp://127.0.0.1:4840
  python scripts/smoke_opcua.py --endpoint opc.tcp://opcua-imitator:4840 --expect-min 90 --check-readonly
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

NAMESPACE_URI = "urn:opcua-imitator"


async def browse_variables(node, ns_idx: int, depth: int, out: list, max_depth: int = 6) -> None:
    if depth > max_depth:
        return
    for child in await node.get_children():
        try:
            nid = child.nodeid
            if nid.NamespaceIndex == ns_idx:
                try:
                    await child.read_value()
                    out.append(child)
                    continue
                except Exception:
                    pass
            await browse_variables(child, ns_idx, depth + 1, out, max_depth)
        except Exception:
            continue


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--endpoint",
        default=os.getenv("OPC_UA_SMOKE_ENDPOINT", "opc.tcp://127.0.0.1:4840"),
        help="endpoint имитатора (по умолчанию OPC_UA_SMOKE_ENDPOINT или opc.tcp://127.0.0.1:4840)",
    )
    ap.add_argument("--expect-min", type=int, default=50, help="минимум переменных в дереве")
    ap.add_argument("--sample", type=int, default=5, help="сколько переменных напечатать с значениями")
    ap.add_argument("--check-readonly", action="store_true", help="проверить, что внешняя запись отклоняется")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    try:
        from asyncua import Client
        from asyncua.ua.uaerrors import UaError
    except ImportError:
        print("НЕТ ЗАВИСИМОСТИ: asyncua не установлена (pip install asyncua)", file=sys.stderr)
        return 2

    print(f"endpoint: {args.endpoint}")
    async with Client(url=args.endpoint, timeout=args.timeout) as client:
        idx = await client.get_namespace_index(NAMESPACE_URI)
        if idx == 0:
            print(f"ОШИБКА: пространство имён {NAMESPACE_URI!r} не зарегистрировано", file=sys.stderr)
            return 1
        print(f"namespace {NAMESPACE_URI!r} -> index {idx}")

        objects = client.get_objects_node()
        found: list = []
        await browse_variables(objects, idx, 0, found)
        print(f"переменных в пространстве имён имитатора: {len(found)}")
        if len(found) < args.expect_min:
            print(f"ОШИБКА: ожидалось не меньше {args.expect_min}", file=sys.stderr)
            return 1

        for node in found[: args.sample]:
            try:
                name = (await node.read_browse_name()).Name
                value = await node.read_value()
                print(f"  {node.nodeid.to_string():<70} {name}={value!r}")
            except Exception as exc:  # pragma: no cover - диагностика
                print(f"  {node.nodeid.to_string()} чтение не удалось: {exc}")

        if args.check_readonly:
            target = found[0]
            try:
                await target.write_value(True)
                print(f"ВНИМАНИЕ: внешняя запись прошла в {target.nodeid.to_string()} — ожидался отказ")
                return 1
            except UaError as exc:
                print(f"запись отклонена как ожидалось: {type(exc).__name__}: {exc}")
            except Exception as exc:
                print(f"запись отклонена ({type(exc).__name__}: {exc})")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
