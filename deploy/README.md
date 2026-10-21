# CI/CD: opcuaimitator.ru

`main` → `.github/workflows/deploy-vps.yml`:

1. **test** — `pytest` бэкенда и сборка фронтенда тем же node, что в `Dockerfile`;
2. **build + push** — образ `ghcr.io/krotkikhmaxim/opcua-imitator:sha-<12>` (+ `:latest`) с кэшем GHA;
3. **deploy → VPS** — `ssh` ограниченным ключом, `app=imitator image=<тег>`.

Ручной запуск: `workflow_dispatch` без параметров собирает выбранный ref;
`image=ghcr.io/krotkikhmaxim/opcua-imitator:sha-…` деплоит готовый образ
(откат на предыдущую версию без пересборки).

## Что делает сервер

Deploy-ключ на `153.80.185.111` не имеет шелла: `authorized_keys` содержит
`restrict,command="/usr/local/bin/asutp-deploy"`. Скрипт (та же логика, что в
`asutpv1-bigllm/deploy/vps/asutp-deploy.sh`):

- `docker pull` образа (токен GHCR приходит в stdin через ssh);
- `docker compose -f compose.prod.yml -f compose.debug.yml up -d --no-deps imitator` —
  пересоздаётся только контейнер имитатора; тома состояния в `/opt/asutp/state/imitator`;
- ждёт `healthy` и ответ `https://opcuaimitator.ru/` (302 от Authelia) до 240 с;
- при провале возвращает образ из `/opt/asutp/state/deploy/imitator.image`;
- пишет в `/opt/asutp/releases/deploy.log`.

Ручной откат:

```sh
ssh srv170637 "app=imitator image=$(sed -n 1p /opt/asutp/state/deploy/imitator.image)"
```

## Режим manager (WP-F)

`compose.prod.yml` запускает контейнер в `IMITATOR_ROLE=manager`: процесс
держит дефолтный экземпляр (:8000/:4840, анонимный — станция не
перенастраивается) и поднимает по личному дочернему uvicorn-процессу на
аутентифицированного пользователя (HTTP `127.0.0.1:8101+i`, OPC UA
`0.0.0.0:4841+i`, состояние в `/opt/asutp/state/imitator/data/users/`).
Схема ответа `GET /internal/instances` — `imitator.instances.v1`,
заморожена (см. `docs/server_state.md` §12).

Значения берутся из `deploy/.env`:

| Переменная | Назначение |
| --- | --- |
| `IMITATOR_ROLE` | `manager` (прод) / `single` — прежнее поведение одним процессом |
| `IMITATOR_INTERNAL_TOKEN` | токен `X-Internal-Token` внутреннего API (`/internal/instances`); пусто — API закрыт |
| `IMITATOR_EDGE_TOKEN` | при заданном значении `Remote-*` доверяются только вместе с `X-Edge-Token`; иначе заголовки trusted (за Caddy+Authelia так и есть) |
| `IMITATOR_MAX_INSTANCES` | ёмкость дочерних экземпляров (8; сверх — `503 instance_limit`) |
| `IMITATOR_IDLE_TIMEOUT_S` | останов простаивающего экземпляра (1800 с) |

`OPC_UA_SERVER_USER` / `OPC_UA_SERVER_PASSWORD` в `.env` контейнера не
задавать: их inject'ит менеджер только дочерним процессам, дефолтная
полоса должна остаться анонимной.

## Секреты репозитория

`VPS_HOST`, `VPS_PORT`, `VPS_USER`, `VPS_SSH_KEY` (приватный ed25519 деплоя),
`VPS_HOST_KEY` (строка known_hosts `[153.80.185.111]:2222 ssh-ed25519 …`).
