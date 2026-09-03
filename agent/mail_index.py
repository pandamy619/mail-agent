# -*- coding: utf-8 -*-
"""
Локальный индекс писем (SQLite, стандартная библиотека).

Зачем: читать заголовки всей истории (20+ тыс. писем) с IMAP-сервера на
каждый запрос долго и накладно для сервера.
Индекс строится ОДИН раз (scripts/build_index.py), дальше дообновляется
бесплатно при каждом обычном скане — и поиск по всей истории становится
мгновенным.

Хранятся только карточки: отправитель, тема, дата, статус, стабильный id.
Тексты писем в индекс не попадают. Индекс — карта, а не территория:
перед любым действием письма сверяются с живой Почтой по id.
"""
import re
import sqlite3
import time
from pathlib import Path

from .log import get as _log

DB_PATH = Path(__file__).resolve().parents[1] / "state" / "index.db"


def _norm(s: str) -> str:
    """Нормализация для поиска: нижний регистр, без пробелов/точек/дефисов.
    «MTS Link», «mts-link», «mts.link», «MTSLink» → «mtslink»."""
    return re.sub(r"[\s.\-_]+", "", (s or "").lower())


def _age_str(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{max(1, sec // 60)} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    if sec < 86400 * 60:
        return f"{sec // 86400} дн назад"
    return f"{sec // (86400 * 30)} мес назад"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        account  TEXT NOT NULL,
        mid      INTEGER NOT NULL,
        sender   TEXT, subject TEXT,
        sender_l TEXT, subject_l TEXT,
        sender_n TEXT, subject_n TEXT,
        received REAL, unread INTEGER,
        PRIMARY KEY (account, mid))""")
    con.execute("""CREATE INDEX IF NOT EXISTS ix_recv
                   ON messages(account, received DESC)""")
    con.execute("""CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY, value TEXT)""")
    # миграция старой базы: добавить нормализованные колонки и заполнить их
    cols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
    if "sender_n" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN sender_n TEXT")
        con.execute("ALTER TABLE messages ADD COLUMN subject_n TEXT")
        rows = con.execute(
            "SELECT account, mid, sender, subject FROM messages").fetchall()
        con.executemany(
            "UPDATE messages SET sender_n = ?, subject_n = ? "
            "WHERE account = ? AND mid = ?",
            [(_norm(s), _norm(sub), a, m) for a, m, s, sub in rows])
        con.commit()
        _log().info(f"index: миграция нормализации — {len(rows)} карточек")
    return con


def upsert(account: str, rows: list, scan_time: float = None) -> int:
    """Добавить/обновить карточки писем. rows — как из mail.scan()."""
    if not rows:
        return 0
    scan_time = scan_time or time.time()
    data = []
    for r in rows:
        try:
            mid = int(r["id"])
        except (KeyError, TypeError, ValueError):
            continue
        sender = r.get("sender", "")
        subject = r.get("subject", "")
        data.append((account, mid, sender, subject,
                     sender.lower(), subject.lower(),
                     _norm(sender), _norm(subject),
                     scan_time - float(r.get("age_sec", 0)),
                     1 if r.get("unread") else 0))
    if not data:
        return 0
    con = _conn()
    con.executemany("""INSERT INTO messages
        (account, mid, sender, subject, sender_l, subject_l,
         sender_n, subject_n, received, unread)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account, mid) DO UPDATE SET
        sender=excluded.sender, subject=excluded.subject,
        sender_l=excluded.sender_l, subject_l=excluded.subject_l,
        sender_n=excluded.sender_n, subject_n=excluded.subject_n,
        received=excluded.received, unread=excluded.unread""", data)
    con.commit()
    con.close()
    return len(data)


def _add_text_filter(where: list, params: list, field: str, value) -> None:
    """Фильтр по обычной ИЛИ нормализованной форме: «mts.link» найдёт
    «MTS Link <invitation@mts-link.ru>»."""
    if not value:
        return
    raw = str(value).lower()
    normed = _norm(raw)
    if normed:
        where.append(f"({field}_l LIKE ? OR {field}_n LIKE ?)")
        params.extend([f"%{raw}%", f"%{normed}%"])
    else:
        where.append(f"{field}_l LIKE ?")
        params.append(f"%{raw}%")


def search(sender_contains: str = None, subject_contains: str = None,
           account: str = None, unread_only: bool = False,
           limit: int = 15, offset: int = 0) -> dict:
    """Мгновенный поиск по всей истории. Возвращает {"total": N, "rows": [...]}."""
    where, params = [], []
    if account:
        where.append("account = ?")
        params.append(account)
    _add_text_filter(where, params, "sender", sender_contains)
    _add_text_filter(where, params, "subject", subject_contains)
    if unread_only:
        where.append("unread = 1")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    con = _conn()
    total = con.execute(f"SELECT COUNT(*) FROM messages {w}", params).fetchone()[0]  # noqa: S608
    cur = con.execute(
        f"SELECT account, mid, sender, subject, received, unread "  # noqa: S608
        f"FROM messages {w} ORDER BY received DESC LIMIT ? OFFSET ?",
        params + [int(limit), int(offset)])
    now = time.time()
    rows = [{"account": a, "id": m, "sender": s or "", "subject": sub or "",
             "received": rcv, "unread": bool(u),
             "age_str": _age_str(now - (rcv or now))}
            for a, m, s, sub, rcv, u in cur.fetchall()]
    con.close()
    return {"total": total, "rows": rows}


def search_ids(account: str, sender_contains: str = None,
               subject_contains: str = None, cap: int = 10000,
               older_days: int = 0) -> list:
    """ВСЕ id писем ящика по фильтру (свежие первыми) — для массовых заявок
    и оценки объёма авто-уборки. older_days > 0 — только старше N дней."""
    where, params = ["account = ?"], [account]
    _add_text_filter(where, params, "sender", sender_contains)
    _add_text_filter(where, params, "subject", subject_contains)
    if older_days and int(older_days) > 0:
        where.append("received < ?")
        params.append(time.time() - int(older_days) * 86400)
    con = _conn()
    cur = con.execute(
        f"SELECT mid FROM messages WHERE {' AND '.join(where)} "  # noqa: S608
        f"ORDER BY received DESC LIMIT ?", params + [int(cap)])
    out = [r[0] for r in cur.fetchall()]
    con.close()
    return out


def get_by_ids(account: str, ids: list) -> dict:
    """{mid: {"sender":…, "subject":…}} для предпросмотра заявок."""
    if not ids:
        return {}
    con = _conn()
    marks = ",".join("?" for _ in ids)
    cur = con.execute(
        f"SELECT mid, sender, subject FROM messages "  # noqa: S608
        f"WHERE account = ? AND mid IN ({marks})",
        [account] + [int(i) for i in ids])
    out = {m: {"sender": s or "", "subject": sub or ""}
           for m, s, sub in cur.fetchall()}
    con.close()
    return out


def delete_by_filter(account: str, sender_contains: str = None,
                     subject_contains: str = None) -> int:
    """Убрать из индекса все карточки по фильтру (после живой операции:
    часть удалена из Почты, часть могла держать протухшие id — свежие
    вернутся при ближайшем скане)."""
    where, params = ["account = ?"], [account]
    _add_text_filter(where, params, "sender", sender_contains)
    _add_text_filter(where, params, "subject", subject_contains)
    if len(where) == 1:
        return 0   # без фильтра не чистим весь ящик
    con = _conn()
    cur = con.execute(
        f"DELETE FROM messages WHERE {' AND '.join(where)}", params)  # noqa: S608
    con.commit()
    n = cur.rowcount
    con.close()
    return n


def delete_ids(account: str, ids: list) -> int:
    """Убрать карточки (например, после подтверждённого удаления писем)."""
    if not ids:
        return 0
    con = _conn()
    marks = ",".join("?" for _ in ids)
    cur = con.execute(
        f"DELETE FROM messages WHERE account = ? AND mid IN ({marks})",  # noqa: S608
        [account] + [int(i) for i in ids])
    con.commit()
    n = cur.rowcount
    con.close()
    return n


def known_ids(account: str) -> set:
    """Все id писем ящика, которые уже есть в индексе."""
    con = _conn()
    out = {r[0] for r in con.execute(
        "SELECT mid FROM messages WHERE account = ?", [account]).fetchall()}
    con.close()
    return out


def counts() -> dict:
    """Статистика индекса: {account: {"total": n, "unread": u, "oldest_days": d}}."""
    con = _conn()
    cur = con.execute("""SELECT account, COUNT(*), SUM(unread), MIN(received)
                         FROM messages GROUP BY account""")
    now = time.time()
    out = {}
    for acc, n, u, oldest in cur.fetchall():
        out[acc] = {"total": n, "unread": int(u or 0),
                    "oldest_days": int((now - oldest) // 86400) if oldest else 0}
    con.close()
    return out


def is_ready(account: str = None) -> bool:
    """Есть ли в индексе данные (по ящику или вообще)."""
    con = _conn()
    if account:
        n = con.execute("SELECT COUNT(*) FROM messages WHERE account = ?",
                        [account]).fetchone()[0]
    else:
        n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.close()
    return n > 0


def meta_get(key: str, default: str = "") -> str:
    con = _conn()
    row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
    con.close()
    return row[0] if row else default


def meta_set(key: str, value: str) -> None:
    con = _conn()
    con.execute("INSERT INTO meta VALUES(?, ?) ON CONFLICT(key) "
                "DO UPDATE SET value = excluded.value", [key, str(value)])
    con.commit()
    con.close()


def clear_account(account: str) -> None:
    con = _conn()
    con.execute("DELETE FROM messages WHERE account = ?", [account])
    con.execute("DELETE FROM meta WHERE key LIKE ?", [f"%:{account}"])
    con.commit()
    con.close()
