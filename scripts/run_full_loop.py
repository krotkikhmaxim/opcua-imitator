#!/usr/bin/env python
"""Полный контур: сценарий → снапшот станции в окне остановки → расчёт → сверка.

Автоматизирует то, что оператор делает руками из веб-интерфейса:

  1. убедиться, что станция отвечает за прокси имитатора;
  2. запустить один эпизод сценария (по умолчанию ``estop_console``);
  3. дождаться фазы ``stopped`` — машина стоит, причина ещё нажата
     (окно HOLD_MS = 20 с; после восстановления причина уйдёт и сверка
     причины стала бы ложным расхождением);
  4. взять последнюю запись журнала правды и попросить сверку —
     снапшот станции (live_batch), расчёт и отчёт;
  5. напечатать сводку.

Код возврата 0 только при ``verdict: pass``; 1 — расхождение; 2 — контур
не собрался (станция недоступна, сценарий не запустился, окно пропущено).
Один и тот же SEED даёт один и тот же прогон.

Пример: ``make loop SEED=7`` или
``python scripts/run_full_loop.py --kind inverter_fault --seed 11``.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

DEFAULT_API = "http://127.0.0.1:8000"
DEFAULT_KIND = "estop_console"
STOP_TIMEOUT_S = 180.0
POLL_S = 0.5


class LoopError(Exception):
    """Контур не собрался: код 2, продолжать не на чем."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def call(api: str, method: str, path: str, body: Optional[Dict] = None, timeout: float = 15.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        api.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = exc.reason
        raise LoopError("api_error", f"{method} {path} → {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LoopError("unreachable", f"{method} {path} → имитатор недоступен: {exc.reason}") from exc


def require_station(api: str) -> None:
    try:
        call(api, "GET", "/api/station/health", timeout=10)
    except LoopError as exc:
        if exc.code == "api_error":
            raise  # станция ответила ошибкой — покажем её, а не «недоступна»
        raise LoopError(
            "station_unreachable",
            "станция не отвечает за прокси имитатора — поднимите её (make install-stack "
            "в репо станции) и проверьте STATION_URL",
        ) from exc


def wait_stopped_window(api: str, deadline_s: float) -> Dict:
    """Дождаться фазы ``stopped``: единственное окно, в котором честно сверять.

    К концу прогона (``finished``) причина уже восстановлена — снапшот
    против неё был бы ложным расхождением. Окно держится 20 секунд
    (HOLD_MS), опрос каждые полсекунды успевает в него с запасом.
    """
    started = time.monotonic()
    while time.monotonic() - started < deadline_s:
        status = call(api, "GET", "/api/scenarios/status")
        if status.get("error"):
            raise LoopError("scenario_error", f"сценарий ответил ошибкой: {status['error']}")
        if status.get("phase") == "stopped":
            return status
        if not status.get("running"):
            raise LoopError(
                "missed_window",
                "сценарий закончился, не попав в окно stopped — "
                "снимайте снапшот в окне (20 с после остановки машины)",
            )
        time.sleep(POLL_S)
    raise LoopError("timeout", f"фаза stopped не наступила за {deadline_s:.0f} с")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API, help="база имитатора")
    parser.add_argument("--kind", default=DEFAULT_KIND, help="сценарий эпизода")
    parser.add_argument("--seed", type=int, default=7, help="повторяемый прогон")
    args = parser.parse_args()

    try:
        require_station(args.api)
        print("1/4  станция отвечает")

        started = call(
            args.api,
            "POST",
            "/api/scenarios/start",
            {"kinds": [args.kind], "once": True, "seed": args.seed},
        )
        print(f"2/4  сценарий {args.kind} запущен (seed {started.get('seed')})")

        status = wait_stopped_window(args.api, STOP_TIMEOUT_S)
        print(f"3/4  машина стоит (эпизод {status.get('episode')}, запись {status.get('writes')}) — окно сверки")

        journal = call(args.api, "GET", "/api/scenarios/journal?limit=1")
        if not journal:
            raise LoopError("no_journal", "журнал правды пуст — эпизод не записан")
        entry = journal[0]

        report = call(args.api, "POST", "/api/verification/report", {"entry": entry}, timeout=120)
    except LoopError as exc:
        print(f"ОШИБКА [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    causes = report.get("cause_check", {}).get("counts", {})
    state = report.get("state_check", {})
    outputs = report.get("outputs") or {}
    print(
        f"4/4  сверка: причины {json.dumps(causes, ensure_ascii=False)}; "
        f"состояние {state.get('value_matched')}/{state.get('present')} значений совпали, "
        f"{state.get('missing')} отсутствуют; "
        f"выходы станции {outputs.get('computed')}/{outputs.get('total')} (наблюдение)"
    )
    for verdict in report.get("cause_check", {}).get("verdicts", []):
        print(
            f"     причина {verdict.get('signal_id')}: {verdict.get('verdict')} "
            f"(ожидалось {verdict.get('expected')!r}, в снапшоте {verdict.get('actual')!r})"
        )
    print(f"итог: {report.get('verdict')}")

    return 0 if report.get("verdict") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
