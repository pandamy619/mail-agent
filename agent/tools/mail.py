# -*- coding: utf-8 -*-
"""
Инструменты Почты (Mail.app) — этап 2: ТОЛЬКО ЧТЕНИЕ.

Правила проекта:
- Каждый запрос — всегда в конкретном ящике (см. PLAN.md).
- Пользовательские строки в AppleScript не попадают: имя ящика берётся
  только из реального списка аккаунтов, фильтры поиска работают в Python,
  в скрипты подставляются лишь целые числа.

Скорость (выводы бенчмарков 24.08.2026):
- Обращения к единому «Входящие» стоят 7–17 с каждое — не используем.
- По-аккаунтные ящики быстрые (subject×25: 0.8–2.5 с), но поштучные
  чтения стоят ~0.03–0.1 с/письмо, поэтому:
  * направление списка определяем двумя запросами дат и сканируем ОДНО
    узкое окно, а не оба конца;
  * для непрочитанных сначала забираем только статусы (один батч),
    детали — лишь по найденным письмам;
  * поиск в два прохода: батч «отправитель+тема» → фильтр в Python →
    детали только по совпавшим.
"""
import subprocess
import time

from .. import mail_index
from ..log import get as _log

FS = "\x1f"  # разделитель полей
RS = "\x1e"  # разделитель записей

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


class MailError(Exception):
    """Общая ошибка при обращении к Почте."""


class MailNotAuthorized(MailError):
    """macOS не разрешает управлять Почтой (Автоматизация)."""


def _run(script: str, timeout: int = 180, label: str = "osascript") -> str:
    lg = _log()
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        lg.warning(f"{label}: TIMEOUT {timeout} с")
        raise MailError(
            f"Почта не ответила за {timeout} с. Попробуйте ещё раз — "
            "первый запрос к ящику бывает долгим."
        )
    dt = time.monotonic() - t0
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        low = err.lower()
        lg.warning(f"{label}: {dt:.2f} с FAIL: {err[:300]}")
        if "1743" in err or "not authorized" in low or "не разреш" in low:
            raise MailNotAuthorized(err)
        raise MailError(err or f"osascript завершился с кодом {p.returncode}")
    lg.debug(f"{label}: {dt:.2f} с OK, {len(p.stdout)} байт")
    return p.stdout


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Аккаунты ─────────────────────────────────────────────────────────

_accounts_cache = None


def accounts_info(refresh: bool = False) -> list:
    """Ящики Почты с адресами: [{"name": "Google", "email": "a@b"}].
    Кэшируется на время работы."""
    global _accounts_cache
    if _accounts_cache is None or refresh:
        raw = _run('''
on run
    set fs to character id 31
    set rs to character id 30
    set out to ""
    tell application "Mail"
        repeat with a in accounts
            set em to "?"
            try
                set em to item 1 of (email addresses of a)
            end try
            try
                if em is "?" then set em to user name of a
            end try
            set out to out & (name of a) & fs & em & rs
        end repeat
    end tell
    return out
end run''', label="accounts_info")
        rows = []
        for rec in raw.split(RS):
            parts = rec.split(FS)
            if len(parts) == 2 and parts[0].strip():
                rows.append({"name": parts[0].strip(),
                             "email": parts[1].strip()})
        _accounts_cache = rows
    return _accounts_cache


def list_accounts(refresh: bool = False) -> list:
    """Точные имена ящиков (аккаунтов) Почты."""
    return [a["name"] for a in accounts_info(refresh)]


def account_email(name: str) -> str:
    """Адрес почты ящика по его имени (или '?', если Почта его не отдала)."""
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


# ── AppleScript-заготовки ────────────────────────────────────────────

def _mb_lookup(account_esc: str) -> str:
    """Найти «Входящие» аккаунта. Правильный путь — mailboxes of inbox;
    конструкции «inbox of account» в словаре Mail нет."""
    return f'''set mb to missing value
        try
            repeat with amb in (mailboxes of inbox)
                if name of account of amb is "{account_esc}" then
                    set mb to amb
                    exit repeat
                end if
            end repeat
        end try
        if mb is missing value then
            try
                set mb to mailbox "INBOX" of account "{account_esc}"
            end try
        end if
        if mb is missing value then
            try
                set mb to mailbox "Inbox" of account "{account_esc}"
            end try
        end if
        if mb is missing value then error "не нашёл «Входящие» ящика {account_esc}"'''


def _dir_block(window: int) -> str:
    """Определить направление списка (2 запроса дат) и выбрать одно окно."""
    return f'''set n to count of messages of mb
        if n = 0 then return "EMPTY"
        set k to {int(window)}
        if k > n then set k to n
        set fwd to true
        if n > 1 then
            set d1 to date received of message 1 of mb
            set dn to date received of message n of mb
            if dn > d1 then set fwd to false
        end if
        if fwd then
            set lo to 1
            set hi to k
        else
            set lo to n - k + 1
            set hi to n
        end if'''


_HEADER = '''on run
    set fs to character id 31
    set rs to character id 30
    set nowd to current date
    tell application "Mail"
'''
_FOOTER = '''
        return out
    end tell
end run'''


def _scan_script(window: int, acc: str) -> str:
    return _HEADER + f'''        {_mb_lookup(acc)}
        {_dir_block(window)}
        set mids to id of messages lo thru hi of mb
        set rds to read status of messages lo thru hi of mb
        set snds to sender of messages lo thru hi of mb
        set subjs to subject of messages lo thru hi of mb
        set dts to date received of messages lo thru hi of mb
        set out to ""
        repeat with j from 1 to (count of rds)
            try
                set out to out & (lo + j - 1) & fs & (item j of mids) & fs & (nowd - (item j of dts)) & fs & (item j of rds) & fs & (item j of snds) & fs & (item j of subjs) & rs
            end try
        end repeat''' + _FOOTER


def _unread_script(window: int, limit: int, acc: str) -> str:
    return _HEADER + f'''        {_mb_lookup(acc)}
        {_dir_block(window)}
        set rds to read status of messages lo thru hi of mb
        set cnt to count of rds
        set picked to {{}}
        if fwd then
            repeat with j from 1 to cnt
                if (item j of rds) is false then
                    set end of picked to (lo + j - 1)
                    if (count of picked) = {int(limit)} then exit repeat
                end if
            end repeat
        else
            repeat with j from cnt to 1 by -1
                if (item j of rds) is false then
                    set end of picked to (lo + j - 1)
                    if (count of picked) = {int(limit)} then exit repeat
                end if
            end repeat
        end if
        set out to ""
        repeat with p in picked
            try
                set m to message p of mb
                set out to out & p & fs & (id of m) & fs & (nowd - (date received of m)) & fs & "false" & fs & (sender of m) & fs & (subject of m) & rs
            end try
        end repeat''' + _FOOTER


def _pairs_script(window: int, acc: str) -> str:
    """Отправитель+тема окна, в порядке от свежих к старым."""
    return _HEADER + f'''        {_mb_lookup(acc)}
        {_dir_block(window)}
        set snds to sender of messages lo thru hi of mb
        set subjs to subject of messages lo thru hi of mb
        set cnt to count of snds
        set out to ""
        if fwd then
            repeat with j from 1 to cnt
                set out to out & (lo + j - 1) & fs & (item j of snds) & fs & (item j of subjs) & rs
            end repeat
        else
            repeat with j from cnt to 1 by -1
                set out to out & (lo + j - 1) & fs & (item j of snds) & fs & (item j of subjs) & rs
            end repeat
        end if''' + _FOOTER


def _details_script(acc: str, indices: list) -> str:
    csv = ", ".join(str(int(i)) for i in indices)
    return _HEADER + f'''        {_mb_lookup(acc)}
        set out to ""
        repeat with p in {{{csv}}}
            try
                set m to message p of mb
                set out to out & p & fs & (id of m) & fs & (nowd - (date received of m)) & fs & (read status of m) & fs & (sender of m) & fs & (subject of m) & rs
            end try
        end repeat''' + _FOOTER


# ── Разбор и публичные функции ──────────────────────────────────────

def _age_str(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{max(1, sec // 60)} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"


def _parse_rows(raw: str, acc: str) -> list:
    if raw.strip() == "EMPTY":
        return []
    rows, seen = [], set()
    for rec in raw.split(RS):
        parts = rec.split(FS)
        if len(parts) != 6:
            continue
        idx, mid, age, read, sender, subject = parts
        try:
            i = int(idx)
        except ValueError:
            continue
        if i in seen:
            continue
        seen.add(i)
        try:
            age_sec = float(age)
        except ValueError:
            age_sec = 0.0
        rows.append({
            "idx": i,
            "id": mid.strip(),
            "age_sec": age_sec,
            "age_str": _age_str(age_sec),
            "unread": read.strip().lower() == "false",
            "account": acc,
            "sender": sender.strip(),
            "subject": subject.strip(),
        })
    rows.sort(key=lambda r: r["age_sec"])  # свежие первыми
    return rows


def scan(window: int = 25, account: str = None) -> list:
    """Срез свежих писем «Входящих» указанного ящика, новые первыми.
    Побочно дообновляет локальный индекс (бесплатная инкрементальность)."""
    acc = resolve_account(account)
    raw = _run(_scan_script(window, _esc(acc)),
               label=f"scan {acc} w{int(window)}")
    rows = _parse_rows(raw, acc)
    try:
        mail_index.upsert(acc, rows)
    except Exception as e:  # noqa: BLE001 — индекс не должен ломать чтение
        _log().debug(f"index: upsert после scan не удался: {e}")
    return rows


def list_recent(limit: int = 10, account: str = None) -> list:
    """Последние письма указанного ящика."""
    return scan(window=max(int(limit), 25), account=account)[: int(limit)]


def list_unread(limit: int = 10, window: int = 100, account: str = None) -> list:
    """Непрочитанные среди последних `window` писем указанного ящика."""
    acc = resolve_account(account)
    raw = _run(_unread_script(window, limit, _esc(acc)),
               label=f"unread {acc} w{int(window)}")
    return _parse_rows(raw, acc)[: int(limit)]


def search(sender_contains: str = None, subject_contains: str = None,
           limit: int = 25, window: int = 100, account: str = None) -> list:
    """Поиск по отправителю/теме среди последних `window` писем ящика.

    Два прохода: батч «отправитель+тема» → фильтр в Python → детали
    только по совпавшим (пользовательские строки в AppleScript не попадают).
    """
    acc = resolve_account(account)
    raw = _run(_pairs_script(window, _esc(acc)),
               label=f"search-pairs {acc} w{int(window)}")
    if raw.strip() == "EMPTY":
        return []
    s_snd = (sender_contains or "").lower()
    s_sub = (subject_contains or "").lower()
    matched = []
    for rec in raw.split(RS):
        parts = rec.split(FS)
        if len(parts) != 3:
            continue
        idx, sender, subject = parts
        if s_snd and s_snd not in sender.lower():
            continue
        if s_sub and s_sub not in subject.lower():
            continue
        try:
            matched.append(int(idx))
        except ValueError:
            continue
        if len(matched) >= int(limit):
            break
    if not matched:
        return []
    raw2 = _run(_details_script(_esc(acc), matched),
                label=f"search-details {acc} n{len(matched)}")
    return _parse_rows(raw2, acc)


def count_messages(account: str = None) -> int:
    """Сколько всего писем во «Входящих» ящика."""
    acc = _esc(resolve_account(account))
    script = f'''
on run
    tell application "Mail"
        {_mb_lookup(acc)}
        return (count of messages of mb) as string
    end tell
end run
'''
    raw = _run(script, label=f"count {account}").strip()
    try:
        return int(raw)
    except ValueError:
        raise MailError(f"неожиданный ответ Почты: {raw[:100]}")


def fetch_chunk(account: str, lo: int, hi: int) -> list:
    """Карточки писем позиций lo..hi (для первичной индексации).
    Без определения направления — порядок ящика как есть."""
    acc = resolve_account(account)
    lo, hi = int(lo), int(hi)
    script = f'''
on run
    set fs to character id 31
    set rs to character id 30
    set nowd to current date
    tell application "Mail"
        {_mb_lookup(_esc(acc))}
        set n to count of messages of mb
        if n = 0 then return "EMPTY"
        set lo to {lo}
        set hi to {hi}
        if hi > n then set hi to n
        if lo > hi then return "EMPTY"
        set mids to id of messages lo thru hi of mb
        set rds to read status of messages lo thru hi of mb
        set snds to sender of messages lo thru hi of mb
        set subjs to subject of messages lo thru hi of mb
        set dts to date received of messages lo thru hi of mb
        set out to ""
        repeat with j from 1 to (count of mids)
            try
                set out to out & (lo + j - 1) & fs & (item j of mids) & fs & (nowd - (item j of dts)) & fs & (item j of rds) & fs & (item j of snds) & fs & (item j of subjs) & rs
            end try
        end repeat
        return out
    end tell
end run
'''
    raw = _run(script, timeout=600, label=f"chunk {acc} {lo}-{hi}")
    return _parse_rows(raw, acc)


def fetch_match_chunk(account: str, lo: int, hi: int,
                      with_subject: bool = False,
                      with_age: bool = False) -> list:
    """Лёгкий срез позиций lo..hi для живого фильтр-исполнителя:
    [(pos, id, sender, subject|"", age_sec|None)]. Всё — из одного чтения."""
    acc = resolve_account(account)
    lo, hi = int(lo), int(hi)
    subj_batch = ("set subjs to subject of messages lo thru hi of mb"
                  if with_subject else "set subjs to {}")
    subj_item = ('(item j of subjs)' if with_subject else '""')
    age_batch = ("set dts to date received of messages lo thru hi of mb"
                 if with_age else "set dts to {}")
    age_item = ('(nowd - (item j of dts))' if with_age else '""')
    script = f'''
on run
    set fs to character id 31
    set rs to character id 30
    set nowd to current date
    tell application "Mail"
        with timeout of 600 seconds
            {_mb_lookup(_esc(acc))}
            set n to count of messages of mb
            if n = 0 then return "EMPTY"
            set lo to {lo}
            set hi to {hi}
            if hi > n then set hi to n
            if lo > hi then return "EMPTY"
            set mids to id of messages lo thru hi of mb
            set snds to sender of messages lo thru hi of mb
            {subj_batch}
            {age_batch}
            set out to ""
            repeat with j from 1 to (count of mids)
                try
                    set out to out & (lo + j - 1) & fs & (item j of mids) & fs & (item j of snds) & fs & {subj_item} & fs & {age_item} & rs
                end try
            end repeat
            return out
        end timeout
    end tell
end run
'''
    raw = _run(script, timeout=650, label=f"match-chunk {acc} {lo}-{hi}")
    rows = []
    if raw.strip() == "EMPTY":
        return rows
    for rec in raw.split(RS):
        parts = rec.split(FS)
        if len(parts) != 5:
            continue
        pos, mid, sender, subject, age = parts
        try:
            age_sec = float(age) if age.strip() else None
        except ValueError:
            age_sec = None
        try:
            rows.append((int(pos), int(mid), sender.strip(), subject.strip(),
                         age_sec))
        except ValueError:
            continue
    return rows


def locate_ids(account: str, ids: list, chunk: int = 2000) -> dict:
    """Найти текущие позиции писем по стабильным id: {mid: idx}.

    Идёт по ящику чанками (один Apple-event на чанк) и останавливается,
    как только все найдены. На большом ящике для очень старых писем
    может занять минуты — прогресс пишется в лог.
    """
    acc = resolve_account(account)
    want = {int(i) for i in ids}
    found = {}
    if not want:
        return found
    total = len(want)
    n = count_messages(acc)
    if n > int(chunk):
        _emit_progress(f"Ищу {total} писем в ящике {acc} "
                       f"({_fmt_n(n)} всего)…")
    lo = 1
    while lo <= n and want:
        hi = min(lo + int(chunk) - 1, n)
        script = f'''
on run
    set rs to character id 30
    tell application "Mail"
        with timeout of 600 seconds
            {_mb_lookup(_esc(acc))}
            set n to count of messages of mb
            set hi to {hi}
            if hi > n then set hi to n
            if {lo} > hi then return ""
            set mids to id of messages {lo} thru hi of mb
            set out to ""
            repeat with j from 1 to (count of mids)
                set out to out & (item j of mids) & rs
            end repeat
            return out
        end timeout
    end tell
end run
'''
        raw = _run(script, timeout=650, label=f"locate {acc} {lo}-{hi}")
        for j, rec in enumerate(raw.split(RS)):
            rec = rec.strip()
            if not rec:
                continue
            try:
                mid = int(rec)
            except ValueError:
                continue
            if mid in want:
                found[mid] = lo + j
                want.discard(mid)
        _log().info(f"locate {acc}: позиции {lo}-{hi}, найдено "
                    f"{len(found)}/{total}")
        if want and hi < n:
            _emit_progress(f"Просмотрено {_fmt_n(hi)} из {_fmt_n(n)} · "
                           f"найдено {len(found)} из {total}…")
        lo = hi + 1
    return found


def get_body_by_id(mid: int, account: str = None, max_chars: int = 1500) -> str:
    """Текст письма по стабильному id (ищет по всему ящику при необходимости)."""
    acc = resolve_account(account)
    pos = locate_ids(acc, [int(mid)])
    if int(mid) not in pos:
        raise MailError(f"письмо id {mid} не найдено во «Входящих» {acc} — "
                        "возможно, удалено или перемещено")
    return get_body(pos[int(mid)], account=acc, max_chars=max_chars)


def get_body(idx: int, account: str = None, max_chars: int = 1500) -> str:
    """Текст письма по индексу из результатов ТОГО ЖЕ ящика. Только чтение."""
    acc = _esc(resolve_account(account))
    idx = int(idx)
    max_chars = int(max_chars)
    script = f'''
on run
    tell application "Mail"
        {_mb_lookup(acc)}
        set m to message {idx} of mb
        set c to content of m
        if (count of c) > {max_chars} then set c to text 1 thru {max_chars} of c
        return c
    end tell
end run
'''
    return _run(script, label=f"body {account} #{idx}")
