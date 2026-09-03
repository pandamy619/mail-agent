#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Этап 1 — тест чтения почты. НИЧЕГО в почте не изменяет.

Показывает последние 10 писем со всех ящиков и текст самого свежего
непрочитанного. При первом запуске macOS спросит разрешение
«Terminal хочет управлять Mail» — нажмите «Разрешить».

Запуск из папки проекта:
    python3 scripts/test_mail.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.tools import mail  # noqa: E402

print("— Этап 1: проверка чтения Почты (только чтение) —\n")
print("⏳ Читаю последние письма из единого «Входящие» всех ящиков…")
print("   Если появится диалог про управление Mail — нажмите «Разрешить».\n")

try:
    recent = mail.list_recent(limit=10)
except mail.MailNotAuthorized:
    print("❌ macOS не разрешает Терминалу управлять Почтой.")
    print("   Откройте: Настройки → Конфиденциальность и безопасность → Автоматизация,")
    print("   найдите Terminal и включите переключатель Mail. Затем запустите тест снова.")
    sys.exit(1)
except mail.MailError as e:
    print(f"❌ Ошибка при обращении к Почте:\n   {e}")
    print("\nПришлите этот вывод Клоду — разберёмся.")
    sys.exit(1)

if not recent:
    print("Во «Входящих» не нашлось ни одного письма — странно.")
    print("Пришлите этот вывод Клоду.")
    sys.exit(0)

print(f"✅ Последние {len(recent)} писем (● — непрочитанное):\n")
for m in recent:
    mark = "●" if m["unread"] else "○"
    print(f" {mark} {m['age_str']:>12} | {m['account'][:14]:<14} | "
          f"{m['sender'][:34]:<34} | {m['subject'][:48]}")

unread = [m for m in recent if m["unread"]]
if unread:
    first = unread[0]
    print(f"\n⏳ Текст самого свежего непрочитанного: «{first['subject'][:60]}»")
    try:
        body = mail.get_body(first["idx"], max_chars=400)
        print("─" * 64)
        print((body.strip() or "(пустое тело письма)")[:400])
        print("─" * 64)
    except mail.MailError as e:
        print(f"⚠️  Список прочитался, а тело письма — нет: {e}")
        print("Пришлите вывод Клоду.")
else:
    print("\nСреди последних 10 писем непрочитанных нет — тело письма")
    print("прочитаем на этапе 2 по команде агенту.")

print("\n🎉 ЭТАП 1 ПРОЙДЕН: агент умеет читать почту со всех ящиков.")
print("Пришлите вывод Клоду (содержимое писем можно замазать) — обсудим этап 2.")
