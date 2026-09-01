# OPC UA Imitator

Система сохранения и загрузки состояния OPC UA сигналов в формате JSON для веб-приложения мониторинга и управления подъёмной машиной (ПМ).

Полное техническое задание: [`docs/ТЗ.md`](docs/ТЗ.md).

## Возможности

- 💾 Сохранение текущих значений всех сигналов в JSON-файл
- ▶ Загрузка сохранённого состояния обратно в OPC UA сервер
- 📥/📤 Импорт и экспорт JSON-конфигураций
- 🗑 Управление сохранёнными состояниями (список, удаление)
- 🔄 Автообновление списка состояний (каждые 10 с)
- ⚡ WebSocket-уведомления о загрузке состояния
- 🧪 Встроенный OPC UA **сервер** (по умолчанию) — публикует сигналы как переменные на `opc.tcp://0.0.0.0:4840`
- 🧪 Режим **имитатора** — только кэш, без сетевого сервера

## Стек

| Компонент | Технология |
|-----------|------------|
| Backend | Python 3.8+ / FastAPI / uvicorn / opcua-asyncio |
| Frontend | React 18 / TypeScript / Vite / Axios |
| Хранение | JSON-файлы в `saved_states/` |

## Запуск

### Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# режим со встроенным OPC UA сервером (по умолчанию) — слушает opc.tcp://0.0.0.0:4840
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# режим имитатора (только кэш, без сетевого OPC UA сервера)
OPC_UA_MODE=imitator .venv/bin/uvicorn app.main:app --port 8000

# подключение к реальному OPC UA серверу
OPC_UA_MODE=real OPC_UA_ENDPOINT=opc.tcp://<host>:<port> .venv/bin/uvicorn app.main:app --port 8000
```

Сигналы из `docs/opcua_input_bindings.json` (схема
`telemetry.opcua.input-bindings.v1`) публикуются как переменные во встроенном
сервере: dotted-идентификатор разворачивается в иерархию папок, а NodeId
переменной равен полному IEC-пути (`ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ1_SinhLeftTop`),
как у реального ПЛК. Значения, записанные через веб-интерфейс (имитация),
видны внешним OPC UA клиентам; сами переменные для внешних клиентов read-only.

### Frontend (разработка)

```bash
cd frontend
npm install
npm run dev
```

### Frontend (production-сборка, раздаётся самим backend)

```bash
cd frontend && npm run build
```

После сборки `frontend/dist` раздаётся backend по адресу `/`.

## API

| Метод | Эндпоинт | Назначение | Вход | Выход |
|-------|----------|------------|------|-------|
| GET | `/api/states/list` | Список сохранённых состояний | — | `StateInfo[]` |
| POST | `/api/states/save` | Сохранить текущее состояние | `{name, description}` | `{status, filename, message, signal_count}` |
| POST | `/api/states/load` | Загрузить состояние в OPC UA | `{filename}` | `{status, loaded_count, errors, message}` |
| DELETE | `/api/states/delete/{filename}` | Удалить состояние | `filename` | `{status, message}` |
| POST | `/api/states/export` | Экспорт как скачиваемый файл | `{name, description}` | JSON-файл |
| POST | `/api/states/import` | Импорт из файла | `file` (multipart) | `{status, filename, signal_count, errors}` |
| GET | `/api/signals` | Текущие сигналы и значения | — | `{signals}` |
| WS | `/ws` | WebSocket-уведомления | — | `{type: "state_loaded", loaded_count, errors}` |

Пример JSON-файла состояния см. в [`docs/ТЗ.md`](docs/ТЗ.md) (раздел 4).

## Тесты

```bash
cd backend
python -m pytest
```

## Конфигурация сигналов

Источник тегов — `docs/opcua_input_bindings.json` (входные сигналы ПЛК и слова
PROFIBUS: `id`, `name`, `project_tag`, `opcua_node`, `type`, `channel`, `cabinet`,
`module`, `direction`, `bit`, `device`, `word_address`). Имитатор публикует их
как `ns=2;s=<project_tag>` и позволяет менять значения через веб-интерфейс для
имитации; внешним OPC UA клиентам переменные доступны только для чтения.
В режиме имитатора значения сигналов хранятся в кэше и обновляются при
сохранении/загрузке состояний. Для реального сервера можно указать `node_id`.

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|------------|------------|--------------|
| `SAVED_STATES_DIR` | Директория хранения состояний | `saved_states` |
| `SIGNAL_CONFIG_PATH` | Путь к конфигурации сигналов | `docs/opcua_input_bindings.json` |
| `OPC_UA_MODE` | `server`, `imitator` или `real` | `server` |
| `OPC_UA_ENDPOINT` | URL реального OPC UA сервера (mode `real`) | `opc.tcp://localhost:4840` |
| `OPC_UA_SERVER_ENDPOINT` | Endpoint встроенного OPC UA сервера (mode `server`) | `opc.tcp://0.0.0.0:4840` |
| `OPC_UA_NAMESPACE` | Namespace index реального сервера | `` |
| `CORS_ORIGINS` | Разрешённые origin через запятую | `*` |

## Режим heartbeat (циклическое обновление SourceTimestamp)

Переключаемый режим **heartbeat / cyclic refresh**: при включении фоновая
задача раз в секунду повторно публикует актуальные значения всех сигналов во
встроенный OPC UA сервер, даже если `Value` не изменился, присваивая им текущий
`SourceTimestamp` (UTC) и `StatusCode = Good`. Режим имитирует циклический опрос
источника и нужен для проверки клиентов, которым важна свежесть данных по
timestamp. По умолчанию heartbeat **выключен**.

Управление — через API или кнопку-тумблер `Heartbeat: OFF/ON` в шапке UI:

| Метод | Эндпоинт | Назначение | Вход | Выход |
|-------|----------|------------|------|-------|
| GET | `/api/heartbeat` | Текущее состояние режима | — | `{enabled, supported}` |
| PUT | `/api/heartbeat` | Включить/выключить режим | `{"enabled": true}` | `{enabled, supported}` |

`supported: false` (или `409` при PUT) означает, что приложение запущено не в
режиме встроенного сервера (`OPC_UA_MODE != server`) — heartbeat нечего
обновлять.

### Семантика OPC UA

- `SourceTimestamp` в этом режиме означает время последнего циклического
  подтверждения/обновления значения, а не обязательно момент изменения
  физической величины.
- Значение и OPC UA тип при перезаписи сохраняются; меняется только
  `SourceTimestamp` (монотонно растёт) и статус фиксируется как `Good`.
- В выключенном режиме timestamp обновляется только при фактическом поступлении
  нового значения через API.
- Клиентам, которым нужны уведомления о смене timestamp без смены value,
  может понадобиться фильтр `DataChangeTrigger = StatusValueTimestamp` на
  мониторинге значений.

В будущем планируется настраиваемый интервал (`interval_ms`) и watchdog: если
данные давно не поступали во входной API, вместо бесконечного обновления
свежести возвращать `Bad_NoCommunication` / `Uncertain_LastUsableValue`.
