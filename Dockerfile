# syntax=docker/dockerfile:1
# ============================================================
#  OPC UA Imitator — production image
#  Сборка фронтенда (node) → runtime (python) → запуск без root.
#  UI раздаёт сам backend из /app/frontend/dist (см. backend/app/main.py,
#  путь: Path(__file__).parent.parent.parent / "frontend" / "dist").
# ============================================================

# ---------- этап 1: сборка фронтенда ----------
FROM node:20-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && test -f dist/index.html

# ---------- этап 2: runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPC_UA_MODE=server \
    OPC_UA_SERVER_ENDPOINT=opc.tcp://0.0.0.0:4840

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/app /app/backend/app
COPY backend/configs /app/backend/configs
COPY docs/opcua_input_bindings.json /app/docs/opcua_input_bindings.json
COPY docs/opcua_nodes.md /app/docs/opcua_nodes.md
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist
# дымовой тест кладём внутрь образа: его можно запускать на сервере без хостовых зависимостей
COPY scripts /app/scripts

# непривилегированный пользователь; uid/gid зафиксированы, чтобы права
# на смонтированные каталоги состояния задавались один раз на хосте
RUN groupadd -g 10002 imitator \
 && useradd -u 10002 -g 10002 -M -d /app -s /usr/sbin/nologin imitator \
 && mkdir -p /app/saved_states /app/scenario_runs/verifications /app/playback_uploads /app/data \
 && chown -R 10002:10002 /app

USER 10002:10002
EXPOSE 8000 4840

# /  отдаётся самим приложением (index.html из dist) — годится как признак живости
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4).status==200 else 1)"

# Один процесс, без --reload и без нескольких воркеров:
# встроенный OPC UA-сервер и состояние должны иметь одного владельца.
CMD ["uvicorn", "--app-dir", "backend", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
