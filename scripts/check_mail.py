#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Фоновая проверка почты: разово или циклом. В Docker работает с --loop.
Логика — в agent/proactive.py.

    python3 scripts/check_mail.py -v              # одна проверка и выход
    python3 scripts/check_mail.py --loop          # каждые N минут из конфига
    python3 scripts/check_mail.py --loop --dry-run  # сообщения в терминал, не в Telegram
    python3 scripts/check_mail.py --reset -v      # стереть состояние, базовая линия заново
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config, proactive  # noqa: E402

lg = proactive.lg


def main():
    ap = argparse.ArgumentParser(
        description="Проверка почты: разово или циклом в терминале "
                    "(в Docker используется --loop)")
    ap.add_argument("--loop", action="store_true",
                    help="работать в терминале постоянно: проверка каждые N минут "
                         "из конфига, остановка — Ctrl+C (лог виден, демон не нужен)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="показывать весь лог прямо в терминале")
    ap.add_argument("--dry-run", action="store_true",
                    help="не слать в Telegram — печатать сообщения в терминал")
    ap.add_argument("--reset", action="store_true",
                    help="стереть состояние: следующая проверка заново создаст "
                         "базовую линию (пингов о старых письмах не будет)")
    args = ap.parse_args()

    already_on_screen = any(type(h) is logging.StreamHandler for h in lg.handlers)
    if (args.verbose or args.loop) and not already_on_screen:
        # в цикле лог всегда на экране (если его уже не включил LOG_STDOUT)
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
        lg.addHandler(h)

    if args.reset:
        for f in (proactive.STATE_FILE, proactive.LOCK_FILE):
            try:
                f.unlink()
            except OSError:
                pass
        print("Состояние стёрто — эта проверка создаст базовую линию заново.")

    sender = proactive.tg_send
    if args.dry_run:
        def sender(text, markup=None, parts=None):
            print("\n──── [dry-run] сообщение в Telegram ────")
            print(text)
            if markup:
                print(f"[кнопки: {[b['text'] for row in markup['inline_keyboard'] for b in row]}]")
            print("────────────────────────────────────────")
            return True

    if args.loop:
        interval_min = int(config.load().get("proactive", {})
                           .get("check_interval_min", 15))
        print("═" * 56)
        print(f" Фоновая проверка в терминале — каждые {interval_min} мин")
        print(" Остановить: Ctrl+C")
        print("═" * 56)
        try:
            while True:
                lg.info("=== proactive: запуск проверки ===")
                proactive.run_check(send=sender)
                lg.info(f"=== проверка завершена; следующая через "
                        f"{interval_min} мин ===")
                time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            print("\nОстановлено. Пока!")
    else:
        lg.info("=== proactive: запуск проверки ===")
        proactive.run_check(send=sender)
        lg.info("=== proactive: проверка завершена ===")


if __name__ == "__main__":
    main()
