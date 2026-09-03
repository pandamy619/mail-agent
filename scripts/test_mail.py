#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка подключения к ящикам по IMAP. Ничего не изменяет — только читает.

Для каждого ящика из config.yaml с заполненными данными в .env:
  подключение и время входа, число писем во «Входящих», найденные папки
  корзины и черновиков, поддержка MOVE, последние 5 писем (● непрочитанные)
  и первые строки самого свежего письма.

Запуск:
    python3 scripts/test_mail.py            # все ящики
    python3 scripts/test_mail.py Google     # один ящик
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config  # noqa: E402
from agent.tools import mail  # noqa: E402


def check(acc: str) -> bool:
    print(f"\n── {acc} ──")
    try:
        info = mail.check_connection(acc)
    except (mail.MailError, config.ConfigError) as e:
        print(f"❌ {e}")
        return False
    print(f"✅ подключение за {info['connect_sec']} с, во «Входящих» {info['inbox']} писем")
    print(f"   корзина: {info['trash']}   черновики: {info['drafts']}")
    print(f"   MOVE: {'да' if info['move'] else 'нет (COPY+EXPUNGE)'}, "
          f"UIDPLUS: {'да' if info['uidplus'] else 'нет'}")
    rows = mail.list_recent(limit=5, account=acc)
    if not rows:
        print("   писем нет")
        return True
    for r in rows:
        mark = "●" if r["unread"] else " "
        print(f"   {mark} [{r['id']}] {r['age_str']:>12}  {r['sender'][:38]:38}  {r['subject'][:50]}")
    try:
        body = mail.get_body_by_id(rows[0]["id"], account=acc, max_chars=300)
        print("   ── текст самого свежего письма ──")
        for line in (body or "(пусто)").splitlines()[:6]:
            print(f"   | {line}")
    except mail.MailError as e:
        print(f"   ⚠ текст не прочитался: {e}")
    return True


def main():
    print("— Проверка почты по IMAP (только чтение) —")
    try:
        accounts = ([mail.resolve_account(sys.argv[1])] if len(sys.argv) > 1
                    else mail.list_accounts())
    except (mail.MailError, config.ConfigError) as e:
        print(f"❌ {e}")
        sys.exit(1)
    if not accounts:
        print("❌ Ни одного ящика с заполненными данными в .env (см. .env.example).")
        sys.exit(1)
    ok = all([check(a) for a in accounts])
    print("\n🎉 Почта доступна." if ok else "\n⚠ Не все ящики доступны — см. выше.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
