# -*- coding: utf-8 -*-
"""
Наблюдения за отправителями: «следи за вторым» берёт отправителя письма,
и каждое новое письмо от него уходит пушем в Telegram сразу, без модели
и без тихих часов.

Наблюдение хранит признаки отправителя из письма: адрес, домен (кроме
общих почтовиков) и имя. Совпадение любого признака — письмо от того же
отправителя; новый адрес, совпавший по домену или имени, дописывается
в наблюдение. Файл — data/watches.json.
"""
import json
import re
import time
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from .log import get as _log

WATCH_FILE = Path(__file__).resolve().parents[1] / "data" / "watches.json"

GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "mail.ru", "bk.ru", "list.ru", "inbox.ru",
    "yandex.ru", "ya.ru", "yandex.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "me.com", "mac.com", "yahoo.com", "rambler.ru", "protonmail.com",
    "proton.me",
}
MIN_NAME = 3


def parse_sender(sender: str) -> dict:
    """«Имя <адрес>» → {"name", "address", "domain"} (в нижнем регистре)."""
    name, addr = parseaddr(sender or "")
    addr = addr.lower().strip()
    if not addr and "@" in (sender or ""):
        addr = sender.strip().lower()
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    name = " ".join((name or "").replace('"', "").split()).strip().lower()
    return {"name": name, "address": addr, "domain": domain}


def load() -> list:
    try:
        return json.loads(WATCH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save(items: list) -> None:
    WATCH_FILE.parent.mkdir(exist_ok=True)
    WATCH_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def add(sender: str, account: str = "") -> tuple:
    """Поставить наблюдение по отправителю письма. Возвращает
    (наблюдение, создано ли новое). Повторная постановка на известный
    адрес возвращает существующее."""
    feat = parse_sender(sender)
    if not feat["address"] and not feat["name"]:
        raise ValueError("у письма нет адреса отправителя")
    items = load()
    for w in items:
        if feat["address"] and feat["address"] in w["addresses"]:
            return w, False
    w = {"name": feat["name"] or feat["address"],
         "addresses": [feat["address"]] if feat["address"] else [],
         "domain": feat["domain"] if feat["domain"] not in GENERIC_DOMAINS else "",
         "account": account or "", "created": datetime.now().strftime("%d.%m.%Y"),
         "hits": 0, "last_hit": ""}
    items.append(w)
    save(items)
    _log().info(f"watch: поставлено наблюдение {describe(w)}")
    return w, True


def remove(n: int = None, sender: str = None) -> dict:
    """Снять наблюдение по номеру из списка или по отправителю письма."""
    items = load()
    idx = None
    if n is not None:
        if not 1 <= int(n) <= len(items):
            raise ValueError(f"нет наблюдения №{n}; сейчас их {len(items)}")
        idx = int(n) - 1
    elif sender:
        hit = match(sender, items)
        if hit:
            idx = items.index(hit[0])
    if idx is None:
        raise ValueError("такого наблюдения нет")
    w = items.pop(idx)
    save(items)
    _log().info(f"watch: снято наблюдение {describe(w)}")
    return w


def match(sender: str, items: list = None) -> tuple:
    """(наблюдение, признак) для отправителя письма или None.
    Признак: address, domain или name."""
    feat = parse_sender(sender)
    for w in items if items is not None else load():
        if feat["address"] and feat["address"] in w["addresses"]:
            return w, "address"
    for w in items if items is not None else load():
        if w.get("domain") and feat["domain"] == w["domain"]:
            return w, "domain"
    for w in items if items is not None else load():
        wn = w.get("name", "")
        if len(wn) >= MIN_NAME and feat["name"] and (wn in feat["name"] or feat["name"] in wn):
            return w, "name"
    return None


def record_hit(sender: str) -> tuple:
    """Учесть письмо: счётчик, время, новый адрес. (наблюдение, признак) или None."""
    items = load()
    hit = match(sender, items)
    if not hit:
        return None
    w, how = hit
    feat = parse_sender(sender)
    if feat["address"] and feat["address"] not in w["addresses"]:
        w["addresses"].append(feat["address"])
    w["hits"] = int(w.get("hits", 0)) + 1
    w["last_hit"] = datetime.now().strftime("%d.%m %H:%M")
    save(items)
    return w, how


def describe(w: dict) -> str:
    parts = [f"«{w['name']}»"] if w.get("name") else []
    if w.get("addresses"):
        parts.append(", ".join(w["addresses"]))
    if w.get("domain"):
        parts.append(f"домен {w['domain']}")
    return " · ".join(parts)


def listing() -> list:
    return [f"{i}. {describe(w)} — с {w.get('created', '?')}, сработало {w.get('hits', 0)}"
            + (f", последнее {w['last_hit']}" if w.get("last_hit") else "")
            for i, w in enumerate(load(), 1)]


HOW_LABEL = {"address": "адрес", "domain": "домен", "name": "имя"}
