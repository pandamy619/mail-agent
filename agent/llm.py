# -*- coding: utf-8 -*-
"""
Клиент Ollama — нативный API /api/chat, без внешних зависимостей.

base_url берётся из переменной окружения LLM_BASE_URL (так его задаёт
Docker: из контейнера Ollama на хосте видна как host.docker.internal),
иначе из config.yaml. Хвост «/v1» отбрасывается — он нужен только
OpenAI-совместимым клиентам. Для qwen3 отключаем режим
«размышлений» (think) ради скорости; если сервер/модель его не знает —
повторяем запрос без этого параметра.
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import config
from .log import get as _log


class LLMError(Exception):
    pass


def _root() -> str:
    base = (config.env_get("LLM_BASE_URL")
            or config.load()["llm"]["base_url"]).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def _post(url: str, payload: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise LLMError(f"HTTP {e.code} от Ollama: {body[:400]}")
    except (urllib.error.URLError, OSError) as e:
        raise LLMError(
            f"Ollama не отвечает ({e}). Запущено ли приложение Ollama?"
        )


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def chat(messages: list, tools: list = None) -> dict:
    """Один запрос к модели. Возвращает message: {content, tool_calls?}."""
    lg = _log()
    cfg = config.load()["llm"]
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "think": False,
    }
    if tools:
        payload["tools"] = tools
    t0 = time.monotonic()
    try:
        resp = _post(_root() + "/api/chat", payload)
    except LLMError as e:
        if "think" in str(e).lower():
            payload.pop("think", None)
            resp = _post(_root() + "/api/chat", payload)
        else:
            lg.warning(f"llm {cfg['model']}: FAIL {e}")
            raise
    msg = resp.get("message", {}) or {}
    msg.pop("thinking", None)  # не тащим размышления в историю
    msg["content"] = _strip_think(msg.get("content", ""))
    calls = [tc.get("function", {}).get("name", "?")
             for tc in (msg.get("tool_calls") or [])]
    lg.debug(f"llm {cfg['model']}: {len(messages)} сообщ. → "
             f"{time.monotonic() - t0:.1f} с, tool_calls={calls or '—'}")
    return msg
