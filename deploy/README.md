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

## Секреты репозитория

`VPS_HOST`, `VPS_PORT`, `VPS_USER`, `VPS_SSH_KEY` (приватный ed25519 деплоя),
`VPS_HOST_KEY` (строка known_hosts `[153.80.185.111]:2222 ssh-ed25519 …`).
