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

Долгая операция (минуты на большом ящике) — идёт с прогрессом.
Прервали Ctrl+C — не страшно: прогресс сохранён, повторный запуск
продолжит с места остановки. Дальше индекс поддерживается свежим
автоматически при каждой работе агента.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import mail_index  # noqa: E402
from agent.log import get as get_log  # noqa: E402
from agent.tools import mail  # noqa: E402

lg = get_log()
CHUNK = 400


def index_account(acc: str, rebuild: bool = False) -> None:
    if rebuild:
        mail_index.clear_account(acc)
        print(f"[{acc}] старый индекс снесён")
    n = mail.count_messages(acc)
    key = f"progress:{acc}"
    done_key = f"done:{acc}"
    start = 1
    saved = mail_index.meta_get(key)
    if saved.isdigit() and not rebuild:
        start = int(saved) + 1
    if mail_index.meta_get(done_key) and not rebuild and start > n:
        print(f"[{acc}] уже проиндексирован ({n} писем); "
              f"для пересборки — --rebuild")
        return
    print(f"[{acc}] всего писем: {n}; начинаю с позиции {start}")
    t0 = time.monotonic()
    total_added = 0
    lo = start
    while lo <= n:
        hi = min(lo + CHUNK - 1, n)
        rows = mail.fetch_chunk(acc, lo, hi)
        added = mail_index.upsert(acc, rows)
        total_added += added
        mail_index.meta_set(key, str(hi))
        pct = hi * 100 // n
        rate = hi - start + 1
        elapsed = time.monotonic() - t0
        eta_min = (elapsed / max(rate, 1)) * (n - hi) / 60
        print(f"[{acc}] {hi}/{n} ({pct}%)  +{added} карточек  "
              f"осталось ~{eta_min:.0f} мин")
        lo = hi + 1
    mail_index.meta_set(done_key, time.strftime("%Y-%m-%d %H:%M"))
    print(f"[{acc}] готово: добавлено/обновлено {total_added} карточек "
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
    except mail.MailNotAuthorized:
        print("❌ macOS не разрешает управлять Почтой (Автоматизация).")
        sys.exit(1)
    except mail.MailError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print("— Индексация почты (только чтение) —")
    lg.info(f"=== build_index: старт, ящики {accounts} ===")
    try:
        for acc in accounts:
            index_account(acc, rebuild=args.rebuild)
    except KeyboardInterrupt:
        print("\nПрервано. Прогресс сохранён — повторный запуск продолжит "
              "с места остановки.")
        sys.exit(0)
    except mail.MailError as e:
        print(f"\n❌ Ошибка Почты: {e}\nПрогресс сохранён — запустите ещё раз.")
        sys.exit(1)

    print("\nИтог по индексу:")
    for acc, st in mail_index.counts().items():
        print(f"  {acc}: {st['total']} писем, непрочитанных {st['unread']}, "
              f"история {st['oldest_days']} дн")
    print("\n🎉 Готово. Поиск по всей истории включён — дальше индекс "
          "обновляется сам при работе агента.")


if __name__ == "__main__":
    main()
