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
- 🧪 Режим **имитатора** OPC UA сервера (по умолчанию) — не требует реального сервера

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
pip install -r requirements.txt

# режим имитатора (по умолчанию)
uvicorn app.main:app --host 0.0.0.0 --port 8000

# подключение к реальному OPC UA серверу
OPC_UA_MODE=real OPC_UA_ENDPOINT=opc.tcp://<host>:<port> uvicorn app.main:app --port 8000
```

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

Описание сигналов (id, имя, тип, доступность записи, начальное значение) — в `backend/configs/signals.json`. В режиме имитатора значения сигналов хранятся в кэше и обновляются при сохранении/загрузке состояний. Для реального сервера добавьте `node_id` к каждому сигналу.

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|------------|------------|--------------|
| `SAVED_STATES_DIR` | Директория хранения состояний | `saved_states` |
| `SIGNAL_CONFIG_PATH` | Путь к конфигурации сигналов | `configs/signals.json` |
| `OPC_UA_MODE` | `imitator` или `real` | `imitator` |
| `OPC_UA_ENDPOINT` | URL реального OPC UA сервера | `opc.tcp://localhost:4840` |
| `OPC_UA_NAMESPACE` | Namespace index реального сервера | `` |
| `CORS_ORIGINS` | Разрешённые origin через запятую | `*` |
