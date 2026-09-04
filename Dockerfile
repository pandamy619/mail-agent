# Почтовый агент: один образ для бота и фоновой проверки.
# Зависимостей нет — только стандартная библиотека Python.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_STDOUT=1

# tzdata — чтобы TZ=Europe/Moscow из compose действовал на тихие часы и дайджест
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# непривилегированный пользователь; uid 1000 совпадает с владельцем папки
# проекта на сервере, чтобы файлы в примонтированных state/ data/ logs/
# принадлежали ему же
RUN useradd --create-home --uid 1000 agent
WORKDIR /app

COPY --chown=agent:agent agent/ agent/
COPY --chown=agent:agent interfaces/ interfaces/
COPY --chown=agent:agent scripts/ scripts/
COPY --chown=agent:agent config.yaml ./

RUN mkdir -p state data logs && chown agent:agent state data logs
USER agent

# по умолчанию — бот; фоновая проверка переопределяет command в compose
CMD ["python3", "interfaces/telegram_bot.py"]
