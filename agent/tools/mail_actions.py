# -*- coding: utf-8 -*-
"""
Инструменты ДЕЙСТВИЙ с почтой (по IMAP).

Правила безопасности проекта:
- «Удалить» всегда означает «в корзину» (восстановимо). Безвозвратна
  только очистка корзины — отдельная операция.
- Результат опасной операции проверяется ФАКТОМ (пересчётом на сервере),
  а не кодом возврата.
- Действия идут по UID: трогаются ровно те письма, что были показаны;
  которых уже нет во «Входящих» — не трогаются и считаются «не найдены».
- Массовые операции «все письма по фильтру» идут по живому ящику:
  заголовки читаются пачками, фильтр применяется в Python, совпавшие
  перемещаются сразу — между «нашёл» и «переместил» проходят секунды.
"""
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from .. import mail_index
from ..imap_client import MailError, quote_folder, uid_set
from ..log import get as _log
from . import mail
from .mail import resolve_account, session

MAX_BATCH = 300         # писем за одну ids-заявку
BULK_CAP = 10000        # предел массовой заявки по фильтру
MATCH_CHUNK = 500       # заголовков за одно чтение в живом фильтре
MOVE_CHUNK = 200        # UID за одну команду перемещения


# ── Перемещение по UID ──────────────────────────────────────────────

def _move_uids(sess, ids: list, target: str, label: str) -> list:
    """Переместить письма (UID во «Входящих») в папку target.
    Возвращает UID, которых после операции во «Входящих» больше нет
    (то есть реально перемещённых). Несуществующие пропускаются."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return []
    present = sess.existing(ids, "INBOX")
    todo = [i for i in ids if i in present]
    if not todo:
        return []
    sess.select("INBOX", readonly=False)
    use_move = sess.has_cap("MOVE")
    for i in range(0, len(todo), MOVE_CHUNK):
        part = uid_set(todo[i:i + MOVE_CHUNK])
        if use_move:
            sess.uid("MOVE", part, quote_folder(target), label=f"{label} move")
        else:
            sess.uid("COPY", part, quote_folder(target), label=f"{label} copy")
            sess.uid("STORE", part, "+FLAGS.SILENT", "(\\Deleted)",
                     label=f"{label} flag")
            if sess.has_cap("UIDPLUS"):
                sess.uid("EXPUNGE", part, label=f"{label} expunge")
            else:
                sess.conn().expunge()
        mail._emit_progress(f"Перемещено {min(i + MOVE_CHUNK, len(todo))} "
                            f"из {len(todo)}…")
    still = sess.existing(todo, "INBOX")
    done = [i for i in todo if i not in still]
    _log().info(f"{label}: запрошено {len(ids)}, было в ящике {len(todo)}, "
                f"перемещено {len(done)}")
    return done


def _cleanup_index(acc: str, done_ids: list) -> None:
    if not done_ids:
        return
    try:
        mail_index.delete_ids(acc, done_ids)
    except Exception:  # noqa: BLE001
        pass


def trash_by_ids(account: str, ids: list) -> int:
    """Переместить письма (по id) в корзину. Возвращает, скольких убрал."""
    acc = resolve_account(account)
    ids = [int(i) for i in ids]
    if not ids:
        raise MailError("список писем пуст")
    if len(ids) > MAX_BATCH:
        raise MailError(f"не больше {MAX_BATCH} писем за ids-заявку — "
                        "для «всех писем от X» есть trash_by_filter")
    sess = session(acc)
    done = _move_uids(sess, ids, sess.trash_folder(), f"trash {acc} n{len(ids)}")
    _cleanup_index(acc, done)
    return len(done)


def move_by_ids(account: str, ids: list, mailbox_name: str) -> int:
    """Переместить письма (по id) в папку ящика."""
    acc = resolve_account(account)
    ids = [int(i) for i in ids]
    if not ids:
        raise MailError("список писем пуст")
    if len(ids) > MAX_BATCH:
        raise MailError(f"не больше {MAX_BATCH} писем за ids-заявку — "
                        "для «всех писем от X» есть move_by_filter")
    target = resolve_mailbox(acc, mailbox_name)
    done = _move_uids(session(acc), ids, target, f"move {acc}→{target} n{len(ids)}")
    _cleanup_index(acc, done)
    return len(done)


def mark_read_by_ids(account: str, ids: list) -> int:
    """Пометить письма прочитанными (по id). Обратимо."""
    acc = resolve_account(account)
    ids = sorted({int(i) for i in ids})
    if not ids:
        raise MailError("список писем пуст")
    sess = session(acc)
    present = sess.existing(ids, "INBOX")
    todo = [i for i in ids if i in present]
    if not todo:
        return 0
    sess.select("INBOX", readonly=False)
    sess.uid("STORE", uid_set(todo), "+FLAGS.SILENT", "(\\Seen)",
             label=f"mark_read {acc} n{len(todo)}")
    try:
        mail_index.upsert(acc, mail.fetch_by_ids(acc, todo))
    except Exception:  # noqa: BLE001
        pass
    return len(todo)


# ── Живой фильтр-исполнитель ────────────────────────────────────────

def _norm_text(s: str) -> str:
    return mail_index._norm(s)


def _match(value: str, needle: str, needle_n: str) -> bool:
    if not needle:
        return True
    low = (value or "").lower()
    return needle in low or (bool(needle_n) and needle_n in _norm_text(low))


def _filter_engine(account: str, sender_contains: str, subject_contains: str,
                   target: str, label: str, older_days: int = 0) -> dict:
    acc = resolve_account(account)
    snd = (sender_contains or "").lower().strip()
    sub = (subject_contains or "").lower().strip()
    if not snd and not sub:
        raise MailError("нужен хотя бы один фильтр: отправитель или тема")
    snd_n, sub_n = _norm_text(snd), _norm_text(sub)
    older_days = max(0, int(older_days or 0))
    min_age = older_days * 86400
    sess = session(acc)
    matched, done_ids, seen = 0, [], 0
    total = None
    for total, offset, rows in mail.iter_chunks(acc, chunk=MATCH_CHUNK):
        try:
            mail_index.upsert(acc, rows)
        except Exception:  # noqa: BLE001
            pass
        ids = []
        for r in rows:
            if not _match(r["sender"], snd, snd_n):
                continue
            if sub and not _match(r["subject"], sub, sub_n):
                continue
            if older_days and r.get("age_sec", 0) < min_age:
                continue
            ids.append(r["id"])
        matched += len(ids)
        if matched > BULK_CAP:
            raise MailError(f"совпало больше {BULK_CAP} писем — сузь фильтр")
        if ids:
            done_ids.extend(_move_uids(sess, ids, target,
                                       f"{label} пачка {offset // MATCH_CHUNK + 1}"))
        seen = min(offset + len(rows), total)
        mail._emit_progress(f"Просмотрено {mail._fmt_n(seen)} из {mail._fmt_n(total)} · "
                            f"найдено {matched}, обработано {len(done_ids)}…")
    _cleanup_index(acc, done_ids)
    return {"matched": matched, "done": len(done_ids), "done_ids": done_ids}


def trash_by_filter_live(account: str, sender_contains: str = None,
                         subject_contains: str = None,
                         older_days: int = 0) -> dict:
    """Все письма по фильтру (и старше N дней) → корзина, по живому ящику."""
    acc = resolve_account(account)
    return _filter_engine(acc, sender_contains, subject_contains,
                          session(acc).trash_folder(), f"trash-live {acc}",
                          older_days=older_days)


def move_by_filter_live(account: str, mailbox_name: str,
                        sender_contains: str = None,
                        subject_contains: str = None) -> dict:
    """Все письма по фильтру → папка, по живому ящику."""
    acc = resolve_account(account)
    target = resolve_mailbox(acc, mailbox_name)
    return _filter_engine(acc, sender_contains, subject_contains,
                          target, f"move-live {acc}→{target}")


# ── Корзина ─────────────────────────────────────────────────────────

def count_trash(account: str) -> int:
    acc = resolve_account(account)
    sess = session(acc)
    return sess.select(sess.trash_folder(), readonly=True)


def empty_trash(account: str) -> dict:
    """Безвозвратно очистить корзину ящика (флаг \\Deleted + EXPUNGE).
    Возвращает {"before", "after"} — результат ВСЕГДА проверяется пересчётом."""
    acc = resolve_account(account)
    sess = session(acc)
    trash = sess.trash_folder()
    before = sess.select(trash, readonly=False)
    if before == 0:
        return {"before": 0, "after": 0}
    sess.uid("STORE", "1:*", "+FLAGS.SILENT", "(\\Deleted)",
             label=f"empty_trash {acc} flag")
    conn = sess.conn()
    sess._call(f"empty_trash {acc} expunge", conn.expunge)
    after = before
    for _ in range(4):
        after = sess.select(trash, readonly=True)
        if after == 0:
            break
        time.sleep(1.0)
    _log().info(f"empty_trash {acc}: было {before}, осталось {after}")
    return {"before": before, "after": after}


# ── Папки ───────────────────────────────────────────────────────────

def list_mailboxes(account: str) -> list:
    """Имена папок ящика (для перемещений)."""
    return session(account).selectable_folders()


def resolve_mailbox(account: str, name: str) -> str:
    """Сопоставить имя папки с реальным списком; вернуть каноническое имя."""
    if not name or not str(name).strip():
        raise MailError("нужно имя папки (mailbox); список даёт list_mailboxes")
    boxes = list_mailboxes(account)
    low = str(name).strip().lower()
    for b in boxes:
        if b.lower() == low:
            return b
    matches = [b for b in boxes if low in b.lower()]
    if len(matches) == 1:
        return matches[0]
    tails = [b for b in boxes if b.lower().rsplit("/", 1)[-1] == low]
    if len(tails) == 1:
        return tails[0]
    raise MailError(f"папка «{name}» не найдена в ящике {account}; "
                    f"есть: {', '.join(boxes[:25])}")


# ── Черновики ───────────────────────────────────────────────────────

def _append_draft(acc: str, msg: EmailMessage) -> str:
    sess = session(acc)
    drafts = sess.drafts_folder()
    conn = sess.conn()
    raw = msg.as_bytes()
    sess._call(f"append draft {acc}",
               lambda: conn.append(quote_folder(drafts), "(\\Draft)",
                                   None, raw))
    return drafts


def create_draft(account: str, to: str, subject: str, body: str) -> str:
    """Создать черновик нового письма в папке черновиков ящика.
    Ничего не отправляет — отправка за пользователем из любого клиента."""
    acc = resolve_account(account)
    if not (to or "").strip():
        raise MailError("нужен адрес получателя")
    msg = EmailMessage()
    msg["From"] = mail.account_email(acc)
    msg["To"] = to.strip()
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body or "")
    folder = _append_draft(acc, msg)
    return f"черновик сохранён в папке «{folder}» ящика {acc}; отправка — за пользователем"


def reply_draft(account: str, mid: int, body: str = "") -> str:
    """Сохранить черновик ответа на письмо (по UID) с цитатой оригинала.
    Ничего не отправляет."""
    acc = resolve_account(account)
    orig = mail.get_message(int(mid), account=acc)
    reply_to = orig.get("Reply-To") or orig.get("From") or ""
    if not parseaddr(reply_to)[1]:
        raise MailError("в письме нет адреса отправителя — некому отвечать")
    subject = (orig.get("Subject") or "").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msg = EmailMessage()
    msg["From"] = mail.account_email(acc)
    msg["To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    orig_id = orig.get("Message-ID")
    if orig_id:
        msg["In-Reply-To"] = orig_id
        refs = (orig.get("References") or "").split()
        msg["References"] = " ".join(refs + [orig_id])
    quoted = ""
    try:
        from ..imap_client import extract_text
        text = extract_text(orig.as_bytes(), max_chars=1500)
        if text:
            quoted = "\n".join("> " + line for line in text.splitlines())
    except Exception:  # noqa: BLE001
        pass
    who = orig.get("From") or ""
    content = (body or "").rstrip()
    if quoted:
        content += f"\n\n{who} писал(а):\n{quoted}\n"
    msg.set_content(content)
    folder = _append_draft(acc, msg)
    return f"черновик ответа сохранён в папке «{folder}» ящика {acc}; отправка — за пользователем"
