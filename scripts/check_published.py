#!/usr/bin/env python3
"""Сверка опубликованного адресного пространства с файлом привязок.

Потребитель адресует узлы строкой `ns=<idx>;s=<полный путь>`. Если
опубликованное расходится с файлом привязок — по составу или по индексу
namespace, — привязки на стороне потребителя указывают в пустоту, и заметно это
станет не здесь, а через несколько слоёв, как «нет данных».
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDINGS = ROOT / "docs" / "opcua_input_bindings.json"


def main(api: str) -> int:
    source = json.loads(BINDINGS.read_text(encoding="utf-8"))
    declared = source["bindings"]
    problems = 0

    if source.get("count") != len(declared):
        print(f"файл привязок противоречит себе: count={source.get('count')}, записей {len(declared)}")
        problems += 1

    try:
        with urllib.request.urlopen(f"{api}/api/signals", timeout=10) as response:
            published = json.load(response)["signals"]
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"API {api} недоступен: {error}")
        return 1

    print(f"в файле привязок: {len(declared)} | опубликовано: {len(published)}")

    declared_ids = {row["id"] for row in declared}
    missing = sorted(declared_ids - set(published))
    extra = sorted(set(published) - declared_ids)
    print(f"не опубликовано: {len(missing)} | лишних: {len(extra)}")
    if missing:
        print("  первые не опубликованные:", ", ".join(missing[:5]))
        problems += 1
    if extra:
        print("  первые лишние:", ", ".join(extra[:5]))
        problems += 1

    namespace = source.get("opcua_namespace_index")
    print(
        f"namespace в файле привязок: ns={namespace} — потребитель адресует узлы этим "
        "индексом, и при его смене все привязки укажут в пустоту"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"))
