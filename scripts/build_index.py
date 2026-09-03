#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Первичная индексация почты: выгружает карточки ВСЕХ писем «Входящих»
в локальный индекс (state/index.db), после чего поиск по всей истории
становится мгновенным.

Запуск:
    python3 scripts/build_index.py              # все ящики
    python3 scripts/build_index.py --account Google
    python3 scripts/build_index.py --rebuild    # снести и построить заново

По IMAP заголовки читаются пачками по 500 писем; 20 тысяч писем — минута-две.
Прервали Ctrl+C — не страшно: уже записанные карточки остаются, повторный
запуск докачает только недостающие. Дальше индекс поддерживается свежим
автоматически при каждой работе агента.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import config, mail_index  # noqa: E402
from agent.log import get as get_log  # noqa: E402
from agent.tools import mail  # noqa: E402

lg = get_log()
CHUNK = 500


def index_account(acc: str, rebuild: bool = False) -> None:
    if rebuild:
        mail_index.clear_account(acc)
        print(f"[{acc}] старый индекс снесён")
    sess = mail.session(acc)
    mail._check_uidvalidity(sess, acc)
    uids = sess.all_uids("INBOX")
    known = mail_index.known_ids(acc)
    todo = [u for u in uids if u not in known][::-1]   # новые первыми
    print(f"[{acc}] всего писем: {len(uids)}; в индексе {len(known)}; "
          f"докачать {len(todo)}")
    if not todo:
        mail_index.meta_set(f"done:{acc}", time.strftime("%Y-%m-%d %H:%M"))
        return
    t0 = time.monotonic()
    added = 0
    for i in range(0, len(todo), CHUNK):
        part = todo[i:i + CHUNK]
        rows = sess.fetch_headers(part)
        for r in rows:
            r["account"] = acc
        added += mail_index.upsert(acc, rows)
        done = min(i + CHUNK, len(todo))
        elapsed = time.monotonic() - t0
        eta = elapsed / done * (len(todo) - done)
        print(f"[{acc}] {done}/{len(todo)} ({done * 100 // len(todo)}%)  "
              f"осталось ~{eta / 60:.1f} мин")
    mail_index.meta_set(f"done:{acc}", time.strftime("%Y-%m-%d %H:%M"))
    print(f"[{acc}] готово: добавлено {added} карточек "
          f"за {(time.monotonic() - t0) / 60:.1f} мин")


def main():
    ap = argparse.ArgumentParser(description="Первичная индексация почты")
    ap.add_argument("--account", help="только этот ящик (по умолчанию — все)")
    ap.add_argument("--rebuild", action="store_true",
                    help="снести индекс ящика и построить заново")
    args = ap.parse_args()

    try:
        accounts = ([mail.resolve_account(args.account)] if args.account
                    else mail.list_accounts())
    except (mail.MailError, config.ConfigError) as e:
        print(f"❌ {e}")
        sys.exit(1)
    if not accounts:
        print("❌ Нет ящиков с заполненными данными в .env (см. .env.example).")
        sys.exit(1)

    print("— Индексация почты (только чтение) —")
    lg.info(f"=== build_index: старт, ящики {accounts} ===")
    try:
        for acc in accounts:
            try:
                index_account(acc, rebuild=args.rebuild)
            except (mail.MailError, config.ConfigError) as e:
                print(f"[{acc}] ❌ {e}")
                lg.warning(f"build_index {acc}: {e}")
    except KeyboardInterrupt:
        print("\nПрервано. Записанное сохранено — повторный запуск докачает остальное.")
        sys.exit(0)

    print("\nИтог по индексу:")
    for acc, st in mail_index.counts().items():
        print(f"  {acc}: {st['total']} писем, непрочитанных {st['unread']}, "
              f"история {st['oldest_days']} дн")
    print("\n🎉 Готово. Поиск по всей истории включён — дальше индекс "
          "обновляется сам при работе агента.")


if __name__ == "__main__":
    main()
