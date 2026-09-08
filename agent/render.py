# -*- coding: utf-8 -*-
"""
Рендер списков писем для интерфейсов. Список рисует код из карточек,
которые инструменты вернули за ход (Conversation.turn_cards()), — модель письма
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
    if card.get("reason"):
        lines.append(f"<blockquote>{e(_short(card['reason'], 200))}</blockquote>")
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


# ── Дайджест ────────────────────────────────────────────────────────

COUNT_ONLY = ("Промоакции", "Соцсети", "Форумы")   # в дайджесте — только числом
SENDERS_CAP = 8                                     # строк-отправителей на категорию


def _sender_key(sender: str) -> str:
    s = (sender or "").strip()
    if "<" in s and s.endswith(">"):
        return s[s.rfind("<") + 1:-1].strip().lower()
    return s.lower()


def _sender_name(sender: str) -> str:
    s = (sender or "").strip()
    if "<" in s:
        name = s[: s.rfind("<")].strip(' "')
        return name or s[s.rfind("<") + 1:-1]
    return s


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} {one}"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def _more_senders(n: int) -> str:
    return "…и ещё " + _plural(n, "отправитель", "отправителя", "отправителей")


def aggregate_senders(cards: list) -> list:
    """Карточки → строки по отправителю: {"sender", "count", "latest"},
    свежие отправители первыми."""
    by = {}
    for c in cards:
        key = _sender_key(c.get("sender"))
        row = by.setdefault(key, {"sender": _sender_name(c.get("sender")),
                                  "count": 0, "latest": c, "ids": []})
        row["count"] += 1
        if c.get("id") is not None:
            row["ids"].append(c["id"])
        if (c.get("received") or 0) > (row["latest"].get("received") or 0):
            row["latest"] = c
    return sorted(by.values(), key=lambda r: r["latest"].get("received") or 0,
                  reverse=True)


def digest_blocks(now, cards: list, important: list) -> dict:
    """Структура дайджеста: заголовок, важное, категории по отправителям,
    категории числом. cards — все новые за период, important — важные
    (с reason, preview), исключаются из категорий."""
    imp_ids = {(c.get("account"), c.get("id")) for c in important}
    rest = [c for c in cards if (c.get("account"), c.get("id")) not in imp_ids]
    per_account = {}
    for c in cards:
        per_account[c.get("account") or "?"] = per_account.get(c.get("account") or "?", 0) + 1
    n = 0
    imp = []
    for c in important:
        n += 1
        imp.append(dict(c, n=n))
    groups, counts, count_items = [], [], {}
    for label, group in _groups(rest):
        if label in COUNT_ONLY:
            counts.append((label, len(group)))
            count_items[label] = [{"account": c.get("account"), "id": c.get("id")}
                                  for c in group if c.get("id") is not None]
            continue
        rows = aggregate_senders(group)
        shown = []
        for r in rows[:SENDERS_CAP]:
            n += 1
            shown.append(dict(r, n=n))
        groups.append({"label": label, "total": len(group), "rows": shown,
                       "more": max(0, len(rows) - SENDERS_CAP)})
    return {"when": now.strftime("%d.%m, %H:%M"), "total": len(cards),
            "per_account": per_account, "important": imp,
            "groups": groups, "counts": counts, "count_items": count_items}


TRASHABLE = ("Промоакции",)   # категории с кнопкой «в корзину» под дайджестом


def digest_keyboard(count_items: dict, day: str) -> dict:
    """Кнопки под дайджестом: показать категорию, идущую числом, и убрать
    промо в корзину. Пусто, если нечего показывать."""
    from . import providers
    key_of = {v: k for k, v in providers.LABELS.items()}
    row, actions = [], []
    for label, items in count_items.items():
        if not items or label not in key_of:
            continue
        row.append({"text": f"📂 {label} {len(items)}",
                    "callback_data": f"dg_show:{key_of[label]}:{day}"})
        if label in TRASHABLE:
            actions.append({"text": f"🗑 {label} за период в корзину",
                            "callback_data": f"dg_trash:{key_of[label]}:{day}"})
    rows = [r for r in (row, actions) if r]
    return {"inline_keyboard": rows} if rows else None


def digest_numbered(blocks: dict) -> list:
    """Пронумерованные строки дайджеста для чата бота: одиночные письма
    с id, группы отправителей со списком ids (свежие первыми)."""
    out = []
    for c in blocks["important"]:
        out.append({"n": c["n"], "id": c.get("id"), "ids": [c.get("id")],
                    "account": c.get("account"), "sender": c.get("sender", ""),
                    "subject": c.get("subject", ""), "category": c.get("category", ""),
                    "count": 1})
    for g in blocks["groups"]:
        for r in g["rows"]:
            c = r["latest"]
            ids = sorted({i for i in r.get("ids", []) if i is not None}, reverse=True)
            out.append({"n": r["n"], "id": c.get("id"), "ids": ids or [c.get("id")],
                        "account": c.get("account"), "sender": r["sender"],
                        "subject": c.get("subject", ""), "category": c.get("category", ""),
                        "count": r["count"]})
    return out


NUMBERS_NOTE = "Номера действуют в чате: «покажи третье», «удали второе»."



def digest_html(now, cards: list, important: list) -> list:
    e = html.escape
    b = digest_blocks(now, cards, important)
    head = f"☀️ <b>Дайджест {b['when']}</b>"
    if b["total"]:
        head += f" · новых {b['total']}, важных {len(b['important'])}"
        if len(b["per_account"]) > 1:
            head += " (" + ", ".join(f"{e(a)}: {v}" for a, v in b["per_account"].items()) + ")"
    else:
        head += "\nНовых писем не было — тихо."
    parts = [head]
    if b["important"]:
        parts.append(f"🔔 <b>Важное</b> ({len(b['important'])})")
        parts.extend(card_html(c) for c in b["important"])
    for g in b["groups"]:
        title = f"📂 <b>{e(g['label'])}</b> ({g['total']})" if g["label"] else f"📨 <b>Остальное</b> ({g['total']})"
        lines = [title]
        for r in g["rows"]:
            c = r["latest"]
            times = f" ×{r['count']}" if r["count"] > 1 else ""
            mark = " · ●" if c.get("unread") else ""
            lines.append(f"{r['n']}. <b>{e(_short(r['sender'], 40))}</b>{times}"
                         f" · {e(_when(c))}{mark} · <i>{e(_short(c.get('subject'), 70))}</i>")
        if g["more"]:
            lines.append(_more_senders(g["more"]))
        parts.append("\n".join(lines))
    if b["counts"]:
        parts.append(" · ".join(f"📂 <b>{e(l)}</b>: {v}" for l, v in b["counts"]))
    if b["important"] or b["groups"]:
        parts.append(f"<i>{e(NUMBERS_NOTE)}</i>")
    return parts


def digest_text(now, cards: list, important: list) -> str:
    b = digest_blocks(now, cards, important)
    out = [f"☀️ Дайджест {b['when']} · новых {b['total']}, важных {len(b['important'])}"]
    if not b["total"]:
        out.append("Новых писем не было — тихо.")
    if b["important"]:
        out.append(f"\n🔔 Важное ({len(b['important'])})")
        for c in b["important"]:
            out.append(f" {'●' if c.get('unread') else ' '} {c['n']:>2}. {_short(c.get('sender'), 40):40} "
                       f"{_when(c):>11}  {_short(c.get('subject'), 60)}")
            if c.get("reason"):
                out.append(f"       — {c['reason']}")
            if c.get("preview"):
                out.append(f"       › {c['preview']}")
    for g in b["groups"]:
        out.append(f"\n📂 {g['label'] or 'Остальное'} ({g['total']})")
        for r in g["rows"]:
            c = r["latest"]
            times = f" ×{r['count']}" if r["count"] > 1 else ""
            out.append(f" {'●' if c.get('unread') else ' '} {r['n']:>2}. {_short(r['sender'] + times, 40):40} "
                       f"{_when(c):>11}  {_short(c.get('subject'), 60)}")
        if g["more"]:
            out.append("       " + _more_senders(g["more"]))
    if b["counts"]:
        out.append("\n" + " · ".join(f"📂 {l}: {v}" for l, v in b["counts"]))
    return "\n".join(out)


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
