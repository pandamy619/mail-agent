# -*- coding: utf-8 -*-
"""
Рендер списков писем для интерфейсов. Список рисует код из карточек,
которые инструменты вернули за ход (core.turn_cards()), — модель письма
не перечисляет, только комментирует.

Карточка: {"n", "id", "account", "sender", "subject", "received",
"unread", "category", "preview"?}. Категория — подпись для пользователя
(«Промоакции»); письма группируются по ней в порядке первого появления,
письма без категории идут без заголовка.
"""
import html
from datetime import datetime

PREVIEW_CHARS = 150
TG_CHUNK = 3900


def _when(card: dict) -> str:
    rcv = card.get("received")
    if not rcv:
        return card.get("age_str") or ""
    d = datetime.fromtimestamp(float(rcv))
    now = datetime.now()
    if d.date() == now.date():
        fmt = "%H:%M"
    elif d.year == now.year:
        fmt = "%d.%m %H:%M"
    else:
        fmt = "%d.%m.%y"
    return d.strftime(fmt)


def _groups(cards: list) -> list:
    """[(подпись категории или "", [карточки])] в порядке появления."""
    order, by = [], {}
    for c in cards:
        key = c.get("category") or ""
        if key not in by:
            by[key] = []
            order.append(key)
        by[key].append(c)
    return [(k, by[k]) for k in order]


def _short(s: str, n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ── Telegram (HTML) ─────────────────────────────────────────────────

def card_html(card: dict) -> str:
    e = html.escape
    mark = " · ●" if card.get("unread") else ""
    lines = [f"{card['n']}. <b>{e(_short(card.get('sender'), 60))}</b>"
             f" · {e(_when(card))}{mark}",
             f"   <i>{e(_short(card.get('subject'), 100))}</i>"]
    if card.get("preview"):
        lines.append(f"<blockquote expandable>{e(card['preview'])}</blockquote>")
    return "\n".join(lines)


def cards_html(cards: list) -> list:
    """Части сообщения (заголовки групп и карточки) — упаковывать через pack()."""
    parts = []
    for label, group in _groups(cards):
        if label:
            parts.append(f"📂 <b>{html.escape(label)}</b> ({len(group)})")
        parts.extend(card_html(c) for c in group)
    return parts


def pack(parts: list, limit: int = TG_CHUNK) -> list:
    """Склеить части в сообщения не длиннее limit, не разрывая часть."""
    chunks, cur = [], ""
    for p in parts:
        p = p[:limit]
        if cur and len(cur) + 2 + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


# ── Терминал (текст) ────────────────────────────────────────────────

def cards_text(cards: list) -> str:
    out = []
    for label, group in _groups(cards):
        if label:
            out.append(f"📂 {label} ({len(group)})")
        for c in group:
            mark = "●" if c.get("unread") else " "
            out.append(f" {mark} {c['n']:>2}. {_short(c.get('sender'), 40):40} "
                       f"{_when(c):>11}  {_short(c.get('subject'), 60)}")
            if c.get("preview"):
                out.append(f"       › {c['preview']}")
    return "\n".join(out)
