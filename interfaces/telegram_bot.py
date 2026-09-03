#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-интерфейс почтового агента (этап 4). Без внешних зависимостей —
напрямую через Telegram Bot API (long polling).

Безопасность:
- токен и ваш Telegram-id — только в .env (не в git);
- бот исполняет команды ТОЛЬКО с вашего id; чужие сообщения игнорируются
  и пишутся в лог;
- пока TELEGRAM_USER_ID пуст, бот на любое сообщение отвечает только вашим
  id (чтобы вписать его в .env) и ничего не выполняет;
- код-гейт подтверждений — общий с CLI (живёт в ядре): кнопка [Да] — это
  то же новое сообщение «да» от вас.

Запуск:
    python3 interfaces/telegram_bot.py
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config, core, llm  # noqa: E402
from agent import log as agent_log  # noqa: E402
from agent.tools import mail, mail_actions  # noqa: E402

lg = agent_log.get()

HELP = ("Я ваш почтовый агент. Примеры:\n"
        "— что непрочитанного?\n"
        "— найди письма от GitLab\n"
        "— о чём последнее письмо от Авиасейлс?\n"
        "— письма от гитхаба в корзину\n"
        "— ответь Ивану, что согласен\n\n"
        "Опасные действия выполняются только после вашего «Да».\n"
        "/new — начать диалог заново")

CONFIRM_KB = {"inline_keyboard": [[{"text": "✅ Да", "callback_data": "yes"},
                                   {"text": "❌ Нет", "callback_data": "no"}]]}


class Status:
    """Одно редактируемое статус-сообщение о ходе долгой операции."""

    def __init__(self, bot, chat_id, throttle_sec: float = 4.0):
        self.bot = bot
        self.chat_id = chat_id
        self.throttle = throttle_sec
        self.msg_id = None
        self.last = 0.0

    def __call__(self, text: str):
        now = time.monotonic()
        if self.msg_id is not None and now - self.last < self.throttle:
            return
        self.last = now
        try:
            if self.msg_id is None:
                r = self.bot.api("sendMessage", http_timeout=15,
                                 chat_id=self.chat_id, text="⏳ " + text)
                self.msg_id = r.get("message_id")
            else:
                self.bot.api("editMessageText", http_timeout=15,
                             chat_id=self.chat_id, message_id=self.msg_id,
                             text="⏳ " + text)
        except RuntimeError as e:
            lg.debug(f"telegram: статус не обновился: {e}")

    def finish(self):
        """Убрать статус — его место займёт финальный ответ."""
        if self.msg_id is None:
            return
        try:
            self.bot.api("deleteMessage", http_timeout=15,
                         chat_id=self.chat_id, message_id=self.msg_id)
        except RuntimeError:
            pass
        self.msg_id = None


class Bot:
    def __init__(self, token: str, my_id, default_account: str = None,
                 accounts: list = None):
        self.token = token
        self.my_id = my_id
        self.default_account = default_account
        self.accounts = accounts or []
        self.history = core.new_history(default_account=default_account,
                                        accounts=self.accounts)

    # ── транспорт ───────────────────────────────────────────────────
    def api(self, method: str, http_timeout: int = 65, **params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        req = urllib.request.Request(
            url, data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(f"Telegram {method}: HTTP {e.code} {body[:200]}")
        if not resp.get("ok"):
            raise RuntimeError(f"Telegram {method}: {str(resp)[:200]}")
        return resp["result"]

    def send(self, chat_id, text: str, markup=None):
        text = text or "(пустой ответ)"
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            if markup and i == len(chunks) - 1:
                payload["reply_markup"] = markup
            self.api("sendMessage", http_timeout=30, **payload)

    # ── логика ──────────────────────────────────────────────────────
    def _footer(self, used_accounts: list) -> str:
        """Строка «📮 Ящик (адрес)» по фактическим вызовам инструментов."""
        seen = []
        for raw in used_accounts:
            try:
                canon = mail.resolve_account(raw)
            except mail.MailError:
                continue
            if canon not in seen:
                seen.append(canon)
        if not seen:
            return ""
        parts = []
        for canon in seen:
            em = next((a["email"] for a in self.accounts
                       if a["name"] == canon and a.get("email") not in (None, "", "?")),
                      None)
            parts.append(f"{canon} ({em})" if em else canon)
        return "\n\n📮 " + ", ".join(parts)

    def run_agent(self, chat_id, text: str):
        try:
            self.api("sendChatAction", http_timeout=15,
                     chat_id=chat_id, action="typing")
        except RuntimeError:
            pass
        used = []

        def track(name, args):
            acc = (args or {}).get("account")
            if acc:
                used.append(str(acc))

        status = Status(self, chat_id)
        try:
            reply = core.run_turn(self.history, text, on_tool=track,
                                  on_progress=status)
        except llm.LLMError as e:
            reply = f"Проблема с моделью: {e}"
        except (mail.MailError, config.ConfigError) as e:
            reply = f"Проблема с почтой: {e}"
        status.finish()
        markup = CONFIRM_KB if core.has_pending() else None
        self.send(chat_id, core.plain(reply) + self._footer(used), markup)

    def handle(self, update: dict):
        if "message" in update:
            msg = update["message"]
            uid = msg.get("from", {}).get("id")
            chat_id = msg.get("chat", {}).get("id")
            if self.my_id is None:
                lg.info(f"telegram: сообщение от id {uid}, TELEGRAM_USER_ID не задан")
                self.send(chat_id,
                          f"Ваш Telegram ID: {uid}\n"
                          f"Впишите в .env строку TELEGRAM_USER_ID={uid} "
                          "и перезапустите бота. До этого я ничего не выполняю.")
                return
            if uid != self.my_id:
                lg.warning(f"telegram: ЧУЖОЕ сообщение от id {uid} — игнорирую")
                return
            text = (msg.get("text") or "").strip()
            if not text:
                self.send(chat_id, "Я понимаю только текст.")
                return
            if text == "/start":
                self.send(chat_id, HELP)
                return
            if text == "/new":
                self.history = core.new_history(default_account=self.default_account,
                                                accounts=self.accounts)
                core.cancel_pending()
                self.send(chat_id, "— новый диалог —")
                return
            self.run_agent(chat_id, text)
        elif "callback_query" in update:
            cq = update["callback_query"]
            uid = cq.get("from", {}).get("id")
            chat_id = cq.get("message", {}).get("chat", {}).get("id")
            try:
                self.api("answerCallbackQuery", http_timeout=15,
                         callback_query_id=cq["id"])
            except RuntimeError:
                pass
            if self.my_id is None or uid != self.my_id:
                lg.warning(f"telegram: ЧУЖОЕ нажатие кнопки от id {uid} — игнорирую")
                return
            try:  # убрать кнопки, чтобы не нажать дважды
                self.api("editMessageReplyMarkup", http_timeout=15,
                         chat_id=chat_id,
                         message_id=cq.get("message", {}).get("message_id"))
            except RuntimeError:
                pass
            data = cq.get("data")
            if data in ("cleanup_yes", "cleanup_no"):
                # очистка корзин — детерминированно, без участия модели:
                # кнопку нажал человек, исполняет код с пересчётом
                if data == "cleanup_no":
                    self.send(chat_id, "Ок, корзины не трогаю — спрошу завтра.")
                    return
                lines = []
                for a in mail.accounts_info():
                    try:
                        res = mail_actions.empty_trash(a["name"])
                    except mail.MailError as e:
                        lines.append(f"{a['name']}: ошибка ({str(e)[:80]})")
                        continue
                    if res["before"] > 0:
                        ok = "" if res["after"] == 0 else " ⚠ не всё"
                        lines.append(f"{a['name']}: было {res['before']}, "
                                     f"осталось {res['after']}{ok}")
                lg.info(f"telegram: очистка корзин по кнопке: {lines}")
                self.send(chat_id, "🗑 Очистка корзин:\n"
                          + ("\n".join(lines) or "корзины уже пусты"))
                return
            answer = "да" if data == "yes" else "нет"
            self.run_agent(chat_id, answer)

    def loop(self):
        offset = 0
        while True:
            try:
                updates = self.api("getUpdates", http_timeout=65,
                                   offset=offset, timeout=50)
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                lg.warning(f"telegram: сбой опроса: {e}")
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception as e:  # noqa: BLE001 — бот не должен падать
                    lg.error(f"telegram: ошибка обработки: {e}")


def main():
    env = config.load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("❌ Не задан TELEGRAM_BOT_TOKEN.")
        print("   1) В Telegram: @BotFather → /newbot → скопируйте токен")
        print("   2) cp .env.example .env  и впишите токен")
        print("   Подробности — в SETUP.md, раздел «Этап 4».")
        sys.exit(1)
    uid_raw = env.get("TELEGRAM_USER_ID", "").strip()
    my_id = int(uid_raw) if uid_raw.lstrip("-").isdigit() else None

    default_acc = (config.load().get("mail", {}).get("default_account") or "")
    default_acc = str(default_acc).strip() or None

    accounts = []
    try:
        accounts = mail.accounts_info()
    except (mail.MailError, config.ConfigError) as e:
        print(f"⚠️  Не смог прочитать ящики ({e}) — продолжаю без адресов.")

    bot = Bot(token, my_id, default_account=default_acc, accounts=accounts)
    me = bot.api("getMe", http_timeout=15)
    print("═" * 56)
    print(f" Telegram-бот: @{me.get('username')}  (этап 4)")
    print(f" Доступ: {'только id ' + str(my_id) if my_id else 'id не задан — бот только сообщит ваш id'}")
    print(f" Ящик по умолчанию: {default_acc or 'не задан — агент уточнит'}")
    print(f" Лог: logs/agent.log  |  остановить: Ctrl+C")
    print("═" * 56)
    lg.info(f"=== старт Telegram-бота @{me.get('username')}, id={my_id}, "
            f"ящик={default_acc} ===")
    try:
        bot.loop()
    except KeyboardInterrupt:
        print("\nБот остановлен.")


if __name__ == "__main__":
    main()
