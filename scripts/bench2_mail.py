#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бенчмарк №2 — выбираем быстрый путь чтения почты. Ничего не изменяет.

A. По-аккаунтные ящики вместо единого «Входящие» + все свойства одним событием.
B. Пробное чтение поискового индекса Почты (SQLite) — только открыть и посчитать.

Запуск:
    python3 scripts/bench2_mail.py
"""
import glob
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.tools import mail  # noqa: E402


def timed(label, script, timeout=300):
    t0 = time.monotonic()
    try:
        out = mail._run(script, timeout=timeout)
        print(f"  {label:<50} {time.monotonic() - t0:7.2f} с")
        return (out or "").strip()
    except mail.MailError as e:
        print(f"  {label:<50} {time.monotonic() - t0:7.2f} с  ❌ {str(e)[:70]}")
        return None


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


print("— Бенчмарк №2 —\n")

# ── A. По-аккаунтные ящики ──────────────────────────────────────────
print("A. По-аккаунтные ящики")
raw = timed("имена аккаунтов", 'tell application "Mail" to return name of accounts')
names = [a.strip() for a in (raw or "").split(", ") if a.strip()]
print(f"   аккаунты: {names}\n")

def mb_lookup(q):
    return f'''set mb to missing value
    try
        repeat with amb in (mailboxes of inbox)
            if name of account of amb is "{q}" then
                set mb to amb
                exit repeat
            end if
        end repeat
    end try
    if mb is missing value then set mb to mailbox "INBOX" of account "{q}"'''


for nm in names:
    q = esc(nm)
    timed(f"count входящих «{nm}»", f'''
tell application "Mail"
    {mb_lookup(q)}
    return count of messages of mb
end tell''')
    timed(f"subject 1..25 «{nm}»", f'''
tell application "Mail"
    {mb_lookup(q)}
    set n to count of messages of mb
    if n > 25 then set n to 25
    if n = 0 then return 0
    return count of (subject of messages 1 thru n of mb)
end tell''')
    print()

print("  properties одним событием (все свойства сразу):")
timed("properties 1..25 единого «Входящие»",
      'tell application "Mail" to return count of (properties of messages 1 thru 25 of inbox)')
if names:
    q = esc(names[0])
    timed(f"properties 1..25 «{names[0]}»", f'''
tell application "Mail"
    {mb_lookup(q)}
    set n to count of messages of mb
    if n > 25 then set n to 25
    if n = 0 then return 0
    return count of (properties of messages 1 thru n of mb)
end tell''')

# ── B. Индекс Почты (SQLite) ────────────────────────────────────────
print("\nB. Поисковый индекс Почты (SQLite), только чтение")
cands = glob.glob(os.path.expanduser("~/Library/Mail/V*/MailData/Envelope Index"))
if not cands:
    print("  ❌ Файл индекса не найден в ~/Library/Mail/V*/MailData/")
for p in cands:
    try:
        size = os.path.getsize(p) / 1e6
        print(f"  найден: {p}  ({size:.0f} МБ)")
    except OSError as e:
        print(f"  найден: {p}  (размер недоступен: {e})")

if cands:
    p = cands[0]
    try:
        t0 = time.monotonic()
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' order by 1")]
        print(f"  открылся за {time.monotonic() - t0:.2f} с; таблиц: {len(tables)}")
        print("  таблицы:", ", ".join(tables))
        for t in ("messages", "subjects", "addresses", "mailboxes"):
            if t in tables:
                t0 = time.monotonic()
                n = con.execute(f"select count(*) from {t}").fetchone()[0]  # noqa: S608
                print(f"  count({t}) = {n}   ({time.monotonic() - t0:.3f} с)")
        if "messages" in tables:
            cols = [r[1] for r in con.execute("pragma table_info(messages)")]
            print("  колонки messages:", ", ".join(cols))
        con.close()
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ не открылся: {e}")
        print("     Если ошибка вида 'unable to open database file' или")
        print("     'operation not permitted' — Терминалу нужен «Полный доступ к диску»:")
        print("     Настройки → Конфиденциальность и безопасность → Полный доступ к диску →")
        print("     добавить Terminal, перезапустить Терминал и повторить запуск.")

print("\nГотово. Пришлите весь вывод Клоду — по нему выберем путь.")
