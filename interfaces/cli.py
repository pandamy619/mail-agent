#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Чат с почтовым агентом в терминале (этап 2).

При старте показывает ящики Почты — можно выбрать ящик по умолчанию,
и агент будет работать с ним без переспросов.

Запуск:
    python3 interfaces/cli.py

Команды: /new — начать диалог заново, /exit — выход.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config, core, llm  # noqa: E402
from agent import log as agent_log  # noqa: E402
from agent.tools import mail  # noqa: E402

DIM = "\033[2m"
RED = "\033[31m"
RESET = "\033[0m"

DANGEROUS = {"trash_messages", "move_messages", "trash_by_filter",
             "move_by_filter", "empty_trash", "confirm_action"}


plain = core.plain  # очистка markdown живёт в ядре, общая с Telegram


def show_tool(name, args):
    pretty = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    if name in DANGEROUS:
        print(f"{RED}   ⚠ {name}({pretty[:120]}){RESET}")
    else:
        print(f"{DIM}   → {name}({pretty[:120]}){RESET}")


def pick_default_account():
    """Показать ящики и дать выбрать ящик по умолчанию."""
    print("\n⏳ Читаю ящики из config.yaml и .env…")
    try:
        accs = mail.accounts_info()
    except (mail.MailError, config.ConfigError) as e:
        print(f"⚠️  Не смог получить список ящиков: {e}")
        return None, []
    if not accs:
        print("⚠️  Ни одного ящика с заполненными данными в .env (см. .env.example).")
        return None, []
    print(" Ящики:")
    for i, a in enumerate(accs, 1):
        em = f" — {a['email']}" if a.get("email") and a["email"] != "?" else ""
        print(f"   {i}. {a['name']}{em}")
    try:
        choice = input(" Ящик по умолчанию (номер; Enter — спрашивать каждый раз): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nПока!")
        sys.exit(0)
    if choice.isdigit() and 1 <= int(choice) <= len(accs):
        return accs[int(choice) - 1]["name"], accs
    return None, accs


def main():
    cfg = config.load()
    print("═" * 56)
    print(" Почтовый агент — чтение и действия с подтверждением (IMAP)")
    print(f" Модель: {cfg['llm']['model']}  |  /new — заново, /exit — выход")
    print(f" Лог: {agent_log.LOG_FILE.relative_to(agent_log.LOG_DIR.parent)}")
    print("═" * 56)
    agent_log.get().info(f"=== старт CLI, модель {cfg['llm']['model']} ===")

    default_acc, accounts = pick_default_account()
    if default_acc:
        print(f"\n ✉️  Работаем с ящиком: {default_acc}")
        print("    (другой ящик — просто назовите его в запросе)")
    else:
        print("\n ✉️  Ящик не выбран — агент будет уточнять его в диалоге.")
    print(" Примеры: «что непрочитанного?», «найди письма от GitLab»,")
    print("          «письма от гитхаба в корзину», «ответь Ивану, что согласен»")
    print(" Опасные действия выполняются только после вашего «да».")

    history = core.new_history(default_account=default_acc, accounts=accounts)
    while True:
        try:
            text = input("\nвы: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nПока!")
            return
        if not text:
            continue
        if text.lower() in ("/exit", "/quit", "выход"):
            print("Пока!")
            return
        if text.lower() == "/new":
            history = core.new_history(default_account=default_acc, accounts=accounts)
            core.cancel_pending()
            print("— новый диалог —")
            continue

        t0 = time.monotonic()
        try:
            reply = core.run_turn(
                history, text, on_tool=show_tool,
                on_progress=lambda t: print(f"{DIM}   … {t}{RESET}"))
        except llm.LLMError as e:
            print(f"\n❌ Проблема с моделью: {e}")
            continue
        except (mail.MailError, config.ConfigError) as e:
            print(f"\n❌ Проблема с почтой: {e}")
            continue
        dt = time.monotonic() - t0
        print(f"\nагент: {plain(reply)}")
        print(f"{DIM}   ({dt:.1f} с){RESET}")


if __name__ == "__main__":
    main()
