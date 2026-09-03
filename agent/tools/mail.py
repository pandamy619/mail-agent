# -*- coding: utf-8 -*-
"""
Инструменты почты — ТОЛЬКО ЧТЕНИЕ (по IMAP).

Правила проекта:
- Каждый запрос — всегда в конкретном ящике (см. PLAN.md).
- Письмо адресуется парой (ящик, id), где id — IMAP UID во «Входящих».
  UID стабилен, пока сервер не сменил UIDVALIDITY папки; смену замечает
  scan() и сбрасывает индекс ящика.
- Пользовательские строки на сервер не уходят: фильтры поиска работают
  в Python по индексу либо по свежему срезу заголовков.
"""
import time

from .. import config, imap_client, mail_index
from ..imap_client import MailError  # noqa: F401 — публичное имя инструментов
from ..log import get as _log

# Хук живого прогресса долгих операций (устанавливает core.run_turn):
# callable(text) — интерфейсы показывают текст пользователю.
progress_hook = None


def _fmt_n(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _emit_progress(text: str) -> None:
    hook = progress_hook
    if hook is None:
        return
    try:
        hook(text)
    except Exception as e:  # noqa: BLE001 — прогресс не должен ломать работу
        _log().debug(f"progress_hook: {e}")


# ── Аккаунты ─────────────────────────────────────────────────────────

_accounts_cache = None


def accounts_info(refresh: bool = False) -> list:
    """Ящики с адресами: [{"name": "Google", "email": "a@b"}].
    Берутся из config.yaml; ящики без логина/пароля в .env пропускаются
    с предупреждением в логе. Кэшируется на время работы."""
    global _accounts_cache
    if _accounts_cache is None or refresh:
        rows = []
        for acc in config.accounts():
            try:
                user, _ = config.credentials(acc)
            except config.ConfigError as e:
                _log().warning(f"accounts: {e} — ящик пропущен")
                continue
            rows.append({"name": acc["name"], "email": user})
        _accounts_cache = rows
    return _accounts_cache


def list_accounts(refresh: bool = False) -> list:
    """Точные имена подключённых ящиков."""
    return [a["name"] for a in accounts_info(refresh)]


def account_email(name: str) -> str:
    """Адрес почты ящика по его имени (или '?')."""
    canon = resolve_account(name)
    for a in accounts_info():
        if a["name"] == canon:
            return a["email"]
    return "?"


def resolve_account(name: str) -> str:
    """Сопоставить имя ящика с реальным списком; вернуть каноническое имя."""
    if not name or not str(name).strip():
        raise MailError("нужно указать ящик (account); список даёт list_accounts")
    accs = list_accounts()
    low = str(name).strip().lower()
    for a in accs:
        if a.lower() == low:
            return a
    matches = [a for a in accs if low in a.lower() or a.lower() in low]
    if len(matches) == 1:
        return matches[0]
    raise MailError(f"ящик «{name}» не найден; доступны: {', '.join(accs)}")


def session(account: str) -> imap_client.Session:
    """IMAP-сессия ящика по имени (с проверкой имени)."""
    canon = resolve_account(account)
    for acc in config.accounts():
        if acc["name"] == canon:
            return imap_client.session(acc)
    raise MailError(f"ящик «{account}» отсутствует в config.yaml")


# ── Карточки ────────────────────────────────────────────────────────

def _age_str(sec: float) -> str:
    return mail_index._age_str(sec)


def _finish_rows(rows: list, acc: str) -> list:
    """Дополнить карточки полями агента и упорядочить: свежие первыми."""
    for r in rows:
        r["account"] = acc
        r["age_str"] = _age_str(r.get("age_sec", 0))
    rows.sort(key=lambda r: (r.get("received") or 0, r["id"]), reverse=True)
    return rows


def _check_uidvalidity(sess: imap_client.Session, acc: str) -> None:
    """UIDVALIDITY сменился — старые UID ничего не значат, индекс ящика сброс."""
    try:
        uv = sess.uidvalidity("INBOX")
    except MailError as e:
        _log().debug(f"uidvalidity {acc}: {e}")
        return
    if not uv:
        return
    key = f"uidvalidity:{acc}"
    old = mail_index.meta_get(key)
    if old and old != str(uv):
        _log().warning(f"index {acc}: UIDVALIDITY сменился {old} → {uv}, "
                       "индекс ящика сброшен")
        mail_index.clear_account(acc)
    if old != str(uv):
        mail_index.meta_set(key, str(uv))


def scan(window: int = 25, account: str = None) -> list:
    """Срез свежих писем «Входящих» ящика, новые первыми.
    Побочно дообновляет локальный индекс."""
    acc = resolve_account(account)
    sess = session(acc)
    _check_uidvalidity(sess, acc)
    uids = sess.all_uids("INBOX")
    if not uids:
        return []
    rows = sess.fetch_headers(uids[-int(window):])
    rows = _finish_rows(rows, acc)
    try:
        mail_index.upsert(acc, rows)
    except Exception as e:  # noqa: BLE001 — индекс не должен ломать чтение
        _log().debug(f"index: upsert после scan не удался: {e}")
    return rows


def list_recent(limit: int = 10, account: str = None) -> list:
    """Последние письма указанного ящика."""
    return scan(window=max(int(limit), 25), account=account)[: int(limit)]


def list_unread(limit: int = 10, window: int = 100, account: str = None) -> list:
    """Самые свежие непрочитанные письма ящика (window сохранён для
    совместимости: по IMAP непрочитанные ищутся по всему ящику)."""
    acc = resolve_account(account)
    sess = session(acc)
    uids = sess.search_uids("UNSEEN")
    if not uids:
        return []
    rows = sess.fetch_headers(uids[-int(limit):])
    return _finish_rows(rows, acc)[: int(limit)]


def search(sender_contains: str = None, subject_contains: str = None,
           limit: int = 25, window: int = 200, account: str = None) -> list:
    """Поиск по отправителю/теме среди последних `window` писем ящика —
    запасной путь, пока индекс не построен (фильтр в Python)."""
    rows = scan(window=window, account=account)
    s_snd = (sender_contains or "").lower()
    s_sub = (subject_contains or "").lower()
    out = []
    for r in rows:
        if s_snd and s_snd not in r["sender"].lower():
            continue
        if s_sub and s_sub not in r["subject"].lower():
            continue
        out.append(r)
        if len(out) >= int(limit):
            break
    return out


def count_messages(account: str = None) -> int:
    """Сколько всего писем во «Входящих» ящика."""
    return session(account).select("INBOX", readonly=True)


def fetch_by_ids(account: str, ids: list) -> list:
    """Карточки конкретных писем по UID (для сверки перед действием)."""
    acc = resolve_account(account)
    rows = session(acc).fetch_headers([int(i) for i in ids])
    return _finish_rows(rows, acc)


def iter_chunks(account: str, chunk: int = 500, newest_first: bool = True):
    """Генератор: карточки «Входящих» пачками по `chunk` UID.
    Для первичной индексации и живого фильтра-исполнителя."""
    acc = resolve_account(account)
    sess = session(acc)
    uids = sess.all_uids("INBOX")
    if newest_first:
        uids = uids[::-1]
    for i in range(0, len(uids), int(chunk)):
        part = uids[i:i + int(chunk)]
        yield len(uids), i, _finish_rows(sess.fetch_headers(part), acc)


def get_body_by_id(mid: int, account: str = None, max_chars: int = 1500) -> str:
    """Текст письма по UID. Только чтение (PEEK — статус не меняется)."""
    acc = resolve_account(account)
    raw = session(acc).fetch_body(int(mid))
    return imap_client.extract_text(raw, max_chars=int(max_chars))


def get_message(mid: int, account: str = None):
    """Полное письмо (email.message.EmailMessage) по UID — для ответа."""
    import email
    import email.policy
    acc = resolve_account(account)
    raw = session(acc).fetch_body(int(mid))
    return email.message_from_bytes(raw, policy=email.policy.default)


def check_connection(account: str) -> dict:
    """Проверка ящика: подключение, папки корзины и черновиков, число писем."""
    acc = resolve_account(account)
    sess = session(acc)
    t0 = time.monotonic()
    n = sess.select("INBOX", readonly=True)
    info = {"account": acc, "inbox": n, "connect_sec": round(time.monotonic() - t0, 2),
            "move": sess.has_cap("MOVE"), "uidplus": sess.has_cap("UIDPLUS")}
    for key, fn in (("trash", sess.trash_folder), ("drafts", sess.drafts_folder)):
        try:
            info[key] = fn()
        except MailError as e:
            info[key] = f"? ({e})"
    return info
