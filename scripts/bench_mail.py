#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Замер скорости Почты по частям: что именно тормозит на этом ящике.
Ничего не изменяет. Запуск:
    python3 scripts/bench_mail.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.tools import mail  # noqa: E402


def timed(label, script):
    t0 = time.monotonic()
    try:
        out = mail._run(script, timeout=300)
        dt = time.monotonic() - t0
        print(f"  {label:<44} {dt:7.2f} с")
        return out.strip()
    except mail.MailError as e:
        dt = time.monotonic() - t0
        print(f"  {label:<44} {dt:7.2f} с  ❌ {str(e)[:80]}")
        return None


print("— Бенчмарк Почты (только чтение) —\n")

n = timed("count of messages of inbox",
          'tell application "Mail" to return count of messages of inbox')
print(f"\n  Всего писем во «Входящих»: {n}\n")

for k in (25, 100, 150):
    print(f"  — окно {k} —")
    for prop in ("id", "subject", "sender", "read status", "date received"):
        timed(f"{prop} писем 1..{k} (батч)",
              f'tell application "Mail" to return count of ({prop} of messages 1 thru {k} of inbox)')
    timed(f"имена ящиков 1..{k} (батч-цепочка)",
          f'tell application "Mail" to return count of (name of account of mailbox of messages 1 thru {k} of inbox)')
    print()

print("Готово. Пришлите вывод Клоду.")
