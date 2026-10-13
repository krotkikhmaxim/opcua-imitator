# Запуск имитатора OPC UA.
#
# Одна вещь, ради которой этот файл существует: heartbeat включается только
# вызовом API и живёт в памяти процесса. Без него сервер публикует значения
# один раз и больше их не трогает — потребитель видит соединение, получает
# нотификации и наблюдает замороженный SourceTimestamp, то есть «данные не
# обновляются». `make run` включает его сам.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3
HOST   ?= 0.0.0.0
PORT   ?= 8000
# server (по умолчанию) | imitator — только кэш | real — внешний сервер
MODE   ?=
NPM    ?= npm

VENV    := backend/.venv
UVICORN := $(VENV)/bin/uvicorn
API     := http://127.0.0.1:$(PORT)

# Сценарии: KINDS — через запятую (пусто — все), SEED — повторяемый прогон,
# ONCE=true — один цикл и стоп.
KINDS ?=
SEED  ?=
ONCE  ?= false

.PHONY: help venv run heartbeat check test frontend frontend-build \
	scenario scenario-stop scenario-status scenario-journal scenario-mode \
	playback playback-control loop station-check

help:
	@printf 'make venv   — создать backend/.venv и поставить зависимости\n'
	@printf 'make run    — запустить и включить heartbeat (Ctrl+C — остановить)\n'
	@printf 'make check  — сверить опубликованные узлы с файлом привязок\n'
	@printf 'make test   — pytest\n'
	@printf 'make scenario [KINDS=a,b] [SEED=n] [ONCE=true] — пустить сценарии аварий\n'
	@printf 'make scenario-stop / scenario-status / scenario-journal\n'
	@printf 'make playback XLSX=/path/trend.xlsx — загрузить тренд для плейбэка\n'
	@printf 'make playback-control ACTION=play|pause|resume|stop [RATE=n] [SEEK_MS=n]\n'
	@printf 'make loop [SEED=7] [KIND=estop_console] — полный контур: сценарий → снапшот → сверка\n'
	@printf 'make station-check — доступна ли станция за прокси\n'
	@printf 'make frontend / frontend-build\n\n'
	@printf 'HOST=%s PORT=%s MODE=%s\n' '$(HOST)' '$(PORT)' '$(if $(MODE),$(MODE),server)'

$(VENV):
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet -r backend/requirements.txt

venv: $(VENV)
	@printf 'готово: %s\n' '$(VENV)'

# Сервер уходит в фон только чтобы включить heartbeat, после чего `wait`
# возвращает терминал ему: Ctrl+C останавливает как обычный запуск.
run: $(VENV)
	@$(if $(MODE),OPC_UA_MODE=$(MODE) ,)$(UVICORN) --app-dir backend app.main:app \
		--host $(HOST) --port $(PORT) & \
	pid=$$!; \
	for i in $$(seq 1 60); do \
		curl -fsS -m 2 $(API)/api/heartbeat >/dev/null 2>&1 && break; \
		kill -0 $$pid 2>/dev/null || { printf 'не запустился\n' >&2; exit 1; }; \
		sleep 0.5; \
	done; \
	curl -fsS -m 5 -X PUT $(API)/api/heartbeat -H 'Content-Type: application/json' \
		-d '{"enabled": true}' >/dev/null \
		&& printf '\nheartbeat включён — значения обновляются\n' \
		|| printf '\nВНИМАНИЕ: heartbeat не включился, значения будут заморожены\n' >&2; \
	wait $$pid

heartbeat:
	@curl -fsS -m 5 -X PUT $(API)/api/heartbeat -H 'Content-Type: application/json' \
		-d '{"enabled": true}' >/dev/null && printf 'heartbeat включён\n'

check:
	@$(PYTHON) scripts/check_published.py $(API)

scenario:
	@body='{"once": $(ONCE)'; \
	if [ -n '$(KINDS)' ]; then \
		body="$$body, \"kinds\": [\"$$(printf '%s' '$(KINDS)' | sed 's/,/","/g')\"]"; \
	fi; \
	if [ -n '$(SEED)' ]; then body="$$body, \"seed\": $(SEED)"; fi; \
	curl -fsS -m 5 -X POST $(API)/api/scenarios/start \
		-H 'Content-Type: application/json' -d "$$body}"; echo

scenario-stop:
	@curl -fsS -m 10 -X POST $(API)/api/scenarios/stop; echo

scenario-status:
	@curl -fsS -m 5 $(API)/api/scenarios/status; echo

scenario-journal:
	@curl -fsS -m 5 '$(API)/api/scenarios/journal?limit=20'; echo

# Воспроизвести запись реального режима (id из GET /api/scenarios, поле "mode"):
#   make scenario-mode MODE=drive_fault [SEED=n]
scenario-mode:
	@test -n '$(MODE)' || { echo "usage: make scenario-mode MODE=drive_fault"; exit 2; }
	@body='{"mode": "$(MODE)"'; \
	if [ -n '$(SEED)' ]; then body="$$body, \"seed\": $(SEED)"; fi; \
	curl -fsS -m 5 -X POST $(API)/api/scenarios/start \
		-H 'Content-Type: application/json' -d "$$body}"; echo

# Плейбэк XLSX-тренда: большой файл владельца грузится по пути на сервере,
# не через HTTP. Дальше транспорт — make playback-control ACTION=play…
playback:
	@test -n '$(XLSX)' || { echo "usage: make playback XLSX=/secure/path/trend.xlsx"; exit 2; }
	@test -f '$(XLSX)' || { echo "нет файла: $(XLSX)" >&2; exit 1; }
	@curl -fsS -m 600 -X POST $(API)/api/playback/load_path \
		-H 'Content-Type: application/json' -d "{\"path\": \"$(XLSX)\"}"; echo

playback-control:
	@test -n '$(ACTION)' || { echo "usage: make playback-control ACTION=play"; exit 2; }
	@body="{\"action\": \"$(ACTION)\""; \
	if [ -n '$(RATE)' ]; then body="$$body, \"rate\": $(RATE)"; fi; \
	if [ -n '$(SEEK_MS)' ]; then body="$$body, \"seek_ms\": $(SEEK_MS)"; fi; \
	curl -fsS -m 600 -X POST $(API)/api/playback/control -H 'Content-Type: application/json' -d "$$body}"; echo

# Полный контур: один эпизод сценария → снапшот станции → расчёт → сверка.
# Код 0 только при verdict: pass (см. scripts/run_full_loop.py).
loop:
	@$(VENV)/bin/python scripts/run_full_loop.py --api $(API) --kind '$(if $(KIND),$(KIND),estop_console)' \
		$(if $(SEED),--seed $(SEED),)

station-check:
	@curl -fsS -m 5 $(API)/api/station/health >/dev/null 2>&1 \
		&& printf 'станция отвечает за прокси имитатора\n' \
		|| { printf 'станция недоступна (STATION_URL, сейчас %s)\n' "$$(python3 -c 'import os; print(os.getenv("STATION_URL", "http://127.0.0.1:8080"))')" >&2; exit 1; }

test: $(VENV)
	cd backend && ./.venv/bin/python -m pytest -q

frontend:
	cd frontend && $(NPM) install && $(NPM) run dev

frontend-build:
	cd frontend && $(NPM) install && $(NPM) run build
