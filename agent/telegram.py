# -*- coding: utf-8 -*-
"""
Транспорт Telegram Bot API — один на бота и фоновую проверку.
Без внешних зависимостей: urllib и JSON.
"""
import html
import json
import re
import urllib.error
import urllib.request

from . import config, render
from .log import get as _log

CHUNK = 3900   # запас к лимиту Telegram в 4096 символов


class TelegramError(RuntimeError):
    pass


class Transport:
    def __init__(self, token: str):
        self.token = token

    def api(self, method: str, http_timeout: int = 65, **params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        req = urllib.request.Request(
            url, data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            raise TelegramError(f"Telegram {method}: HTTP {e.code} {body[:200]}")
        if not resp.get("ok"):
            raise TelegramError(f"Telegram {method}: {str(resp)[:200]}")
        return resp["result"]

    def send(self, chat_id, text: str, markup=None):
        """Текст, разрезанный по лимиту; кнопки — к последней части."""
        text = text or "(пустой ответ)"
        chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            if markup and i == len(chunks) - 1:
                payload["reply_markup"] = markup
            self.api("sendMessage", http_timeout=30, **payload)

    def send_html(self, chat_id, parts: list, markup=None):
        """Части с HTML-разметкой, упакованные в сообщения; если Telegram
        разметку отверг — те же части без тегов."""
        chunks = render.pack(parts, CHUNK)
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
            if markup and i == len(chunks) - 1:
                payload["reply_markup"] = markup
            try:
                self.api("sendMessage", http_timeout=30, **payload)
            except TelegramError as e:
                _log().warning(f"telegram: HTML отвергнут ({str(e)[:80]}) — шлю текстом")
                payload.pop("parse_mode")
                payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", chunk))
                self.api("sendMessage", http_timeout=30, **payload)


def from_env():
    """(Transport, chat_id владельца) из TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID,
    иначе (None, None)."""
    token = config.env_get("TELEGRAM_BOT_TOKEN")
    chat = config.env_get("TELEGRAM_USER_ID")
    if not token or not chat.lstrip("-").isdigit():
        return None, None
    return Transport(token), int(chat)


def notify(text: str = "", markup: dict = None, parts: list = None) -> bool:
    """Сообщение владельцу от имени бота (фоновая проверка).
    parts — HTML-части; иначе текст."""
    tg, chat = from_env()
    if tg is None:
        _log().error("telegram: пуш невозможен — нет TELEGRAM_BOT_TOKEN/TELEGRAM_USER_ID")
        return False
    try:
        if parts:
            tg.send_html(chat, parts, markup)
        else:
            tg.send(chat, text[:4000], markup)
        return True
    except Exception as e:  # noqa: BLE001
        _log().warning(f"telegram: недоступен: {e}")
        return False
