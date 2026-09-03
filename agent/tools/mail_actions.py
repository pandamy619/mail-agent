# -*- coding: utf-8 -*-
"""
Инструменты ДЕЙСТВИЙ с Почтой — этап 3.

Правила безопасности проекта:
- «Удалить» всегда означает «в корзину» (восстановимо). Безвозвратна
  только очистка корзины — отдельная операция.
- Результат опасной операции проверяется ФАКТОМ (пересчётом), а не кодом
  возврата: Почта умеет молча игнорировать команды.
- Опасные операции работают по снимку внутренних id писем: между
  «нашёл» и «выполняй» ящик может сдвинуться (придёт новая почта),
  поэтому перед выполнением id заново сверяются со свежим срезом,
  и трогаются только точно те письма.
- Пользовательские строки попадают в AppleScript только экранированными
  (черновики); имена ящиков и папок — только канонические, из реальных
  списков Почты; всё остальное — целые числа.
"""
import time

from .. import mail_index
from ..log import get as _log
from . import mail
from .mail import RS, MailError, _esc, _mb_lookup, _run, resolve_account

MAX_BATCH = 300         # писем за одну ids-заявку / одну партию исполнения
BULK_CAP = 10000        # предел массовой заявки по фильтру

_HEADER = '''on run
    set fs to character id 31
    set rs to character id 30
    tell application "Mail"
        with timeout of 600 seconds
'''
_FOOTER = '''
        end timeout
    end tell
end run'''


# ── Выполнение действий по стабильным id ────────────────────────────
# УРОК ИНЦИДЕНТА 25.08 (в корзину уехало чужое письмо): позиции писем
# валидны только в момент проверки — пока шла локализация, в живой ящик
# пришли новые письма, позиции сдвинулись, удаление по устаревшей позиции
# попало в соседей. Поэтому исполнитель работает ПАРАМИ (позиция, id):
# берёт письмо по позиции и СВЕРЯЕТ id прямо перед действием; не совпал —
# не трогает. Несовпавшие перелоцируются и обрабатываются вторым проходом.
# Из индекса удаляются только реально обработанные письма.
# («delete message p of mb» не компилируется — каприз парсера 24.08 —
# поэтому всегда set m to … → действие над m.)

def _act_pairs_script(acc_esc: str, pairs: list, action_line: str) -> str:
    # set m to (get …) закрепляет ссылку в id-форме (урок 26.08): проверка
    # и действие бьют по одному и тому же письму, даже если позиции сдвинулись
    # между ними — попадание в соседа исключено по построению.
    items = ", ".join(f"{{{int(p)}, {int(mid)}}}" for p, mid in pairs)
    return _HEADER + f'''            {_mb_lookup(acc_esc)}
            set doneIds to ""
            repeat with pr in {{{items}}}
                set p to (item 1 of pr) as integer
                set tid to (item 2 of pr) as integer
                try
                    set m to (get message p of mb)
                    if (id of m) = tid then
                        {action_line}
                        set doneIds to doneIds & tid & rs
                    end if
                end try
            end repeat
            return doneIds''' + _FOOTER


def _engine(account: str, ids: list, action_line: str, label: str) -> dict:
    """Общий исполнитель: локализация → действие со сверкой id → повтор
    для сдвинувшихся. Возвращает {requested, done, left, done_ids}."""
    acc = resolve_account(account)
    ids = [int(i) for i in ids]
    if not ids:
        raise MailError("список писем пуст")
    if len(ids) > BULK_CAP:
        raise MailError(f"слишком много писем ({len(ids)}), предел {BULK_CAP} — "
                        "сузь фильтр")
    remaining = set(ids)
    done_ids = []
    for attempt in (1, 2):
        if not remaining:
            break
        pos = mail.locate_ids(acc, sorted(remaining))
        pos = {mid: p for mid, p in pos.items() if mid in remaining}  # защита
        pairs_all = sorted(((p, mid) for mid, p in pos.items()), reverse=True)
        if not pairs_all:
            break
        for bi in range(0, len(pairs_all), MAX_BATCH):
            part = pairs_all[bi:bi + MAX_BATCH]
            raw = _run(_act_pairs_script(_esc(acc), part, action_line),
                       timeout=600,
                       label=f"{label} проход{attempt} "
                             f"партия{bi // MAX_BATCH + 1}")
            acted = [int(x) for x in raw.split(RS)
                     if x.strip().lstrip("-").isdigit()]
            done_ids.extend(acted)
            remaining.difference_update(acted)
            mail._emit_progress(f"Обработано {len(done_ids)} из {len(ids)}…")
        if remaining and attempt == 1:
            _log().info(f"{label}: {len(remaining)} писем сдвинулись — "
                        "повторная локализация")
            mail._emit_progress(f"{len(remaining)} писем сдвинулись — "
                                "сверяю заново…")
    return {"requested": len(ids), "done": len(done_ids),
            "left": len(remaining), "done_ids": done_ids}


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
    if len(ids) > MAX_BATCH:
        raise MailError(f"не больше {MAX_BATCH} писем за ids-заявку — "
                        "для «всех писем от X» есть trash_by_filter")
    res = _engine(acc, ids, "delete m", f"trash {acc} n{len(ids)}")
    _cleanup_index(acc, res["done_ids"])
    return res["done"]


def move_by_ids(account: str, ids: list, mailbox_name: str) -> int:
    """Переместить письма (по id) в папку ящика."""
    acc = resolve_account(account)
    target = resolve_mailbox(acc, mailbox_name)
    if len(ids) > MAX_BATCH:
        raise MailError(f"не больше {MAX_BATCH} писем за ids-заявку — "
                        "для «всех писем от X» есть move_by_filter")
    line = f'move m to mailbox "{_esc(target)}" of account "{_esc(acc)}"'
    res = _engine(acc, ids, line, f"move {acc}→{target} n{len(ids)}")
    _cleanup_index(acc, res["done_ids"])
    return res["done"]


def mark_read_by_ids(account: str, ids: list) -> int:
    """Пометить письма прочитанными (по id). Обратимо."""
    res = _engine(account, ids, "set read status of m to true",
                  f"mark_read {account} n{len(ids)}")
    return res["done"]


# ── Живой фильтр-исполнитель (урок 26.08: id из индекса протухают) ──
# Массовые операции «все письма по фильтру» не зависят от индекса:
# исполнитель сам идёт по живому ящику чанками ОТ КОНЦА К НАЧАЛУ (удаления
# в высоких позициях не сдвигают низкие), в каждом чанке читает отправителей,
# сверяет фильтр (с нормализацией) и действует над совпавшими сразу — id
# берётся за секунды до действия из того же чтения.

MATCH_CHUNK = 1500


def _match(value: str, needle: str, needle_n: str) -> bool:
    if not needle:
        return True
    low = (value or "").lower()
    return needle in low or (bool(needle_n) and needle_n in _norm_text(low))


def _norm_text(s: str) -> str:
    return mail_index._norm(s)


def _filter_engine(account: str, sender_contains: str, subject_contains: str,
                   action_line: str, label: str,
                   older_days: int = 0) -> dict:
    acc = resolve_account(account)
    snd = (sender_contains or "").lower()
    sub = (subject_contains or "").lower()
    snd_n, sub_n = _norm_text(snd), _norm_text(sub)
    with_subject = bool(sub)
    older_days = max(0, int(older_days or 0))
    min_age = older_days * 86400
    n = mail.count_messages(acc)
    matched = 0
    done_ids = []
    hi = n
    while hi >= 1:
        lo = max(1, hi - MATCH_CHUNK + 1)
        rows = mail.fetch_match_chunk(acc, lo, hi, with_subject=with_subject,
                                      with_age=older_days > 0)
        pairs = []
        for pos, mid, sender, subject, age_sec in rows:
            if not _match(sender, snd, snd_n):
                continue
            if with_subject and not _match(subject, sub, sub_n):
                continue
            if older_days > 0 and (age_sec is None or age_sec < min_age):
                continue
            pairs.append((pos, mid))
        matched += len(pairs)
        pairs.sort(reverse=True)
        for bi in range(0, len(pairs), MAX_BATCH):
            part = pairs[bi:bi + MAX_BATCH]
            raw = _run(_act_pairs_script(_esc(acc), part, action_line),
                       timeout=650, label=f"{label} чанк {lo}-{hi}")
            done_ids.extend(int(x) for x in raw.split(RS)
                            if x.strip().lstrip("-").isdigit())
        mail._emit_progress(f"Просмотрено {n - lo + 1} из {n} · "
                            f"найдено {matched}, обработано {len(done_ids)}…")
        hi = lo - 1
    return {"matched": matched, "done": len(done_ids), "done_ids": done_ids}


def trash_by_filter_live(account: str, sender_contains: str = None,
                         subject_contains: str = None,
                         older_days: int = 0) -> dict:
    """Все письма по фильтру (и старше N дней) → корзина, по живому ящику."""
    acc = resolve_account(account)
    res = _filter_engine(acc, sender_contains, subject_contains,
                         "delete m", f"trash-live {acc}",
                         older_days=older_days)
    try:
        mail_index.delete_by_filter(acc, sender_contains, subject_contains)
    except Exception:  # noqa: BLE001
        pass
    return res


def move_by_filter_live(account: str, mailbox_name: str,
                        sender_contains: str = None,
                        subject_contains: str = None) -> dict:
    """Все письма по фильтру → папка, по живому ящику."""
    acc = resolve_account(account)
    target = resolve_mailbox(acc, mailbox_name)
    line = f'move m to mailbox "{_esc(target)}" of account "{_esc(acc)}"'
    res = _filter_engine(acc, sender_contains, subject_contains,
                         line, f"move-live {acc}→{target}")
    try:
        mail_index.delete_by_filter(acc, sender_contains, subject_contains)
    except Exception:  # noqa: BLE001
        pass
    return res


# ── Корзина ─────────────────────────────────────────────────────────

def _trash_lookup(account_esc: str) -> str:
    """Найти корзину аккаунта (по образцу _mb_lookup, но над trash mailbox)."""
    return f'''set tmb to missing value
        try
            repeat with amb in (mailboxes of trash mailbox)
                if name of account of amb is "{account_esc}" then
                    set tmb to amb
                    exit repeat
                end if
            end repeat
        end try
        if tmb is missing value then
            try
                set tmb to mailbox "Trash" of account "{account_esc}"
            end try
        end if
        if tmb is missing value then
            try
                set tmb to mailbox "Deleted Messages" of account "{account_esc}"
            end try
        end if
        if tmb is missing value then
            try
                set tmb to mailbox "Корзина" of account "{account_esc}"
            end try
        end if
        if tmb is missing value then error "не нашёл корзину ящика {account_esc}"'''


def count_trash(account: str) -> int:
    acc = resolve_account(account)
    script = _HEADER + f'''        {_trash_lookup(_esc(acc))}
        return (count of messages of tmb) as string''' + _FOOTER
    raw = _run(script, label=f"count_trash {acc}").strip()
    try:
        return int(raw)
    except ValueError:
        raise MailError(f"неожиданный ответ Почты: {raw[:100]}")


def _erase_menu_script(account_esc: str) -> str:
    """GUI-автоматизация: Ящик → Стереть удалённые объекты → <аккаунт>.

    Это ЕДИНСТВЕННЫЙ настоящий способ очистить корзину: в AppleScript-словаре
    Почты команды стирания нет, а delete по письмам в корзине Gmail-ящика —
    тихий no-op (выяснено 24.08 по скриншоту корзины). Требует разрешения
    «Универсальный доступ» для Terminal. Имена меню ищем и по-русски,
    и по-английски; диалог подтверждения, если появится, нажимаем сами.
    """
    return f'''
on run
    tell application "Mail" to activate
    delay 0.6
    tell application "System Events"
        tell process "Mail"
            set mbMenu to missing value
            repeat with mname in {{"Ящик", "Mailbox"}}
                try
                    set mbMenu to menu bar item (mname as string) of menu bar 1
                    exit repeat
                end try
            end repeat
            if mbMenu is missing value then error "не нашёл меню Ящик/Mailbox"
            set eraseItem to missing value
            repeat with mi in (menu items of menu 1 of mbMenu)
                try
                    set t to name of mi
                    if t starts with "Стереть удал" or t starts with "Erase Deleted" then
                        set eraseItem to mi
                        exit repeat
                    end if
                end try
            end repeat
            if eraseItem is missing value then error "не нашёл пункт Стереть удалённые объекты"
            set target to missing value
            repeat with smi in (menu items of menu 1 of eraseItem)
                try
                    if (name of smi) contains "{account_esc}" then
                        set target to smi
                        exit repeat
                    end if
                end try
            end repeat
            if target is missing value then error "не нашёл аккаунт {account_esc} в подменю стирания"
            click target
            delay 0.8
            try
                repeat with bname in {{"Стереть", "Erase"}}
                    try
                        click button (bname as string) of sheet 1 of window 1
                        exit repeat
                    end try
                end repeat
            end try
        end tell
    end tell
    return "ok"
end run
'''


def empty_trash(account: str) -> dict:
    """Безвозвратно очистить корзину ящика через меню Почты.

    Возвращает {"before": сколько было, "after": сколько осталось} —
    результат ВСЕГДА проверяется пересчётом, «команда принята» не считается
    успехом.
    """
    acc = resolve_account(account)
    before = count_trash(acc)
    if before == 0:
        return {"before": 0, "after": 0}
    _run(_erase_menu_script(_esc(acc)), timeout=120,
         label=f"erase_menu {acc}")
    after = before
    for _ in range(6):          # ждём применения до ~9 секунд
        time.sleep(1.5)
        after = count_trash(acc)
        if after == 0:
            break
    return {"before": before, "after": after}


# ── Безопасные действия (без подтверждения) ─────────────────────────

def list_mailboxes(account: str) -> list:
    """Имена папок ящика (для перемещений)."""
    acc = resolve_account(account)
    script = _HEADER + f'''        set out to ""
        repeat with mbx in (mailboxes of account "{_esc(acc)}")
            set out to out & (name of mbx) & rs
        end repeat
        return out''' + _FOOTER
    raw = _run(script, label=f"list_mailboxes {acc}")
    return [m.strip() for m in raw.split(RS) if m.strip()]


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
    raise MailError(f"папка «{name}» не найдена в ящике {account}; "
                    f"есть: {', '.join(boxes[:25])}")


def _esc_text(s: str) -> str:
    """Экранировать пользовательский текст для AppleScript-строки,
    включая переводы строк."""
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return s.replace("\r\n", "\n").replace("\n", '" & return & "')


def create_draft(to: str, subject: str, body: str) -> str:
    """Создать черновик нового письма и ОСТАВИТЬ его открытым в Почте.
    Ничего не отправляет."""
    script = f'''
tell application "Mail"
    set msg to make new outgoing message with properties {{subject:"{_esc_text(subject)}", content:"{_esc_text(body)}", visible:true}}
    tell msg to make new to recipient with properties {{address:"{_esc_text(to)}"}}
    activate
end tell
return "ok"'''
    _run(script, label=f"draft → {to}")
    return "черновик открыт в Почте; отправка — за пользователем"


def reply_draft(account: str, idx: int, body: str = "",
                expected_id: int = None) -> str:
    """Открыть окно ответа на письмо idx; при заданном body — вставить текст.
    Ничего не отправляет. expected_id сверяется перед открытием — ответ
    на «соседа» исключён."""
    acc = resolve_account(account)
    body_line = ""
    if body and body.strip():
        body_line = f'''
            try
                set content of r to "{_esc_text(body)}"
            end try'''
    check_line = ""
    if expected_id is not None:
        check_line = (f'\n            if (id of m) is not {int(expected_id)} '
                      'then error "письмо сдвинулось — повторите поиск"')
    script = _HEADER + f'''            {_mb_lookup(_esc(acc))}
            set m to (get message {int(idx)} of mb){check_line}
            set r to reply m opening window yes{body_line}
            activate
            return "ok"''' + _FOOTER
    _run(script, label=f"reply_draft {acc} #{int(idx)}")
    return "окно ответа открыто в Почте; отправка — за пользователем"
