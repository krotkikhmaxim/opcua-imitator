"""Заморозка таймлайна (ТЗ #5, §2, §15.7, §16 — Timeline isolation).

По умолчанию плейбэк трендов не пишет в рабочее состояние сигналов: play и
seek отклоняются с кодом ``frozen``, ни одна строка тренда не доходит до
writer, значения сигналов не меняются. Загрузка и выгрузка источника
остаются доступны — код и данные сохранены для возвращения к задаче.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["OPC_UA_MODE"] = "imitator"

import pytest
from fastapi.testclient import TestClient

import test_playback as fixtures
from app.services.playback.service import PlaybackError, PlaybackService


def test_frozen_service_refuses_play_and_seek(tmp_path):
    """Замороженный таймлайн не доходит ни до одной записи."""

    async def scenario():
        path = fixtures._trend_path(tmp_path, fixtures._short_rows())
        time, writer = fixtures.VirtualTime(), fixtures.FakeWriter()
        svc = PlaybackService(
            writer,
            monotonic=time.monotonic,
            sleep=time.sleep,
            clock=time.clock,
            writes_enabled=False,
        )
        assert svc.status()["writes_enabled"] is False
        svc.load(path, fixtures.CONFIGS, max_bytes=1024 * 1024)

        with pytest.raises(PlaybackError) as play:
            svc.play()
        assert play.value.code == "frozen"
        with pytest.raises(PlaybackError) as seek:
            await svc.seek(300)
        assert seek.value.code == "frozen"

        assert writer.rows == []  # ни одна строка тренда не дошла до writer
        status = svc.status()
        assert status["phase"] == "idle"
        assert status["writes"] == 0
        assert status["loaded"] is True  # источник остался: код и данные сохранены

    asyncio.run(scenario())


def test_frozen_service_keeps_load_and_unload(tmp_path):
    """Заморозка запрещает запись, а не работу с источником."""

    async def scenario():
        path = fixtures._trend_path(tmp_path, fixtures._short_rows())
        svc = PlaybackService(
            fixtures.FakeWriter(),
            monotonic=fixtures.VirtualTime().monotonic,
            writes_enabled=False,
        )
        loaded = svc.load(path, fixtures.CONFIGS, max_bytes=1024 * 1024)
        assert loaded["loaded"] is True
        assert svc.unload()["loaded"] is False

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.getenv("PLAYBACK_WRITES_ENABLED", "").lower() in ("1", "true", "yes", "on"),
    reason="заморозка снята переменной окружения PLAYBACK_WRITES_ENABLED",
)
def test_app_playback_is_frozen_by_default(tmp_path):
    """Развёрнутое приложение: плейбэк заморожен настройкой по умолчанию.

    Значения сверяются через API имитатора; независимым OPC UA-клиентом
    та же проверка выполняется на живом сервере (смоук после рестарта).
    """
    from app.main import app

    path = fixtures._trend_path(tmp_path, fixtures._short_rows())
    with TestClient(app) as client:
        status = client.get("/api/playback/status")
        assert status.status_code == 200
        assert status.json()["writes_enabled"] is False

        before = client.get("/api/signals").json()["signals"]["ch1-10a3-k1"]["value"]

        loaded = client.post("/api/playback/load_path", json={"path": path})
        assert loaded.status_code == 200  # загрузка при заморозке доступна

        played = client.post("/api/playback/control", json={"action": "play"})
        assert played.status_code == 409
        assert "заморожен" in played.json()["detail"]

        seeked = client.post(
            "/api/playback/control", json={"action": "seek", "seek_ms": 300}
        )
        assert seeked.status_code == 409

        after = client.get("/api/signals").json()["signals"]["ch1-10a3-k1"]["value"]
        assert before == after  # значение сигнала не изменилось
