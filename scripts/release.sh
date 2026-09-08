#!/usr/bin/env bash
# Деплой версии из git-тега. Образ собирается из содержимого тега (git archive),
# а не из рабочей копии, поэтому в папке проекта может быть любая ветка.
#
#     scripts/release.sh v0.2.0     # собрать mail-agent:v0.2.0 и перезапустить сервисы
#     scripts/release.sh v0.1.0     # откат: образ уже собран, только перезапуск
#
# Тег записывается в .env (MAIL_AGENT_TAG) — compose подставляет его в image:.
set -euo pipefail
TAG="${1:-}"
if [ -z "$TAG" ]; then
    echo "использование: scripts/release.sh vX.Y.Z"; exit 1
fi
cd "$(dirname "$0")/.."

git fetch -q --tags origin
if ! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "тег $TAG не найден (git fetch --tags)"; exit 1
fi
LOCAL=$(git rev-parse "$TAG^{commit}")
REMOTE=$(git ls-remote --tags origin "refs/tags/$TAG^{}" | cut -f1)
if [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
    echo "тег $TAG локально ($LOCAL) и на origin ($REMOTE) указывают на разные коммиты"; exit 1
fi

if docker image inspect "mail-agent:$TAG" >/dev/null 2>&1; then
    echo "образ mail-agent:$TAG уже собран — использую его"
else
    echo "сборка mail-agent:$TAG из тега ($LOCAL)…"
    git archive --format=tar "$TAG" | docker build -q -t "mail-agent:$TAG" - >/dev/null
fi

if grep -q '^MAIL_AGENT_TAG=' .env 2>/dev/null; then
    sed -i "s/^MAIL_AGENT_TAG=.*/MAIL_AGENT_TAG=$TAG/" .env
else
    printf '\n# версия, которая развёрнута (scripts/release.sh)\nMAIL_AGENT_TAG=%s\n' "$TAG" >> .env
fi

docker compose up -d --remove-orphans
echo
docker compose ps --format 'table {{.Name}}\t{{.Image}}\t{{.Status}}'
