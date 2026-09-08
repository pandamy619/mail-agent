#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-интерфейс почтового агента (этап 4). Без внешних зависимостей —
напрямую через Telegram Bot API (long polling).

Безопасность:
- токен и ваш Telegram-id — в .env или переменных окружения (не в git);
- бот исполняет команды ТОЛЬКО от вашего id и ТОЛЬКО в личном чате с вами
  (в группе ваши же команды игнорируются — иначе почта утекла бы участникам);
  чужие сообщения игнорируются и пишутся в лог;
- кнопка «очистить корзины» действует один раз и только в день вопроса,
  смещение обработанных апдейтов хранится в state/ — после рестарта
  Telegram не заставит бота повторить очистку;
- пока TELEGRAM_USER_ID пуст, бот на любое сообщение отвечает только вашим
  id (чтобы вписать его в .env) и ничего не выполняет;
- код-гейт подтверждений — общий с CLI (живёт в ядре): кнопка [Да] — это
  то же новое сообщение «да» от вас.

Запуск:
    python3 interfaces/telegram_bot.py
"""
import html
import json
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config, core, llm, render, telegram  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
from agent import log as agent_log  # noqa: E402
from agent.tools import mail, mail_actions  # noqa: E402

lg = agent_log.get()

STATE_DIR = Path(__file__).resolve().parents[1] / "state"
OFFSET_FILE = STATE_DIR / "telegram_offset"     # последний ОБРАБОТАННЫЙ update_id + 1
CLEANUP_DONE_FILE = STATE_DIR / "cleanup_done.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

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


def more_kb(token: str) -> dict:
    return {"inline_keyboard": [[{"text": "⬇️ Ещё 10", "callback_data": token}]]}


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
        except telegram.TelegramError as e:
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
        self.tg = telegram.Transport(token)
        self.my_id = my_id
        self.default_account = default_account
        self.accounts = accounts or []
        self.conv = Conversation(default_account=default_account,
                                 accounts=self.accounts)
        self._busy = False     # идёт обработка апдейта (ответ модели)
        self._stop = False     # получен SIGTERM — завершиться после текущего

    # ── транспорт (agent/telegram.py) ───────────────────────────────
    def api(self, method: str, http_timeout: int = 65, **params):
        return self.tg.api(method, http_timeout=http_timeout, **params)

    def send(self, chat_id, text: str, markup=None):
        self.tg.send(chat_id, text, markup)

    def send_html(self, chat_id, parts: list, markup=None):
        self.tg.send_html(chat_id, parts, markup)

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
        self.conv.adopt_digest()
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
            reply = self.conv.run_turn(text, on_tool=track, on_progress=status)
        except llm.LLMError as e:
            reply = f"Проблема с моделью: {e}"
        except (mail.MailError, config.ConfigError) as e:
            reply = f"Проблема с почтой: {e}"
        status.finish()
        cards = self.conv.turn_cards()
        if self.conv.has_pending():
            markup = CONFIRM_KB
        elif cards and self.conv.more_available():
            markup = more_kb(self.conv.last_list["token"])
        else:
            markup = None
        if not cards:
            self.send(chat_id, core.plain(reply) + self._footer(used), markup)
            return
        self._send_cards(chat_id, cards, [html.escape(core.plain(reply))],
                         self._footer(used).strip(), markup)

    def _send_cards(self, chat_id, cards, head_parts, footer, markup):
        try:
            mail.add_previews(cards)
        except Exception as e:  # noqa: BLE001 — превью не должно ломать ответ
            lg.debug(f"telegram: превью не получены: {e}")
        parts = list(head_parts) + render.cards_html(cards)
        if footer:
            parts.append(html.escape(footer))
        self.send_html(chat_id, parts, markup)

    def handle_more(self, chat_id, token: str):
        """Кнопка «Ещё 10»: следующая страница кодом, без модели."""
        cards = self.conv.page_more(token)
        if not cards:
            self.send(chat_id, "Этот список устарел — попросите показать заново.")
            return
        ll = self.conv.last_list
        head = f"Показано {ll['offset'] + ll['shown']} из {ll['total']}"
        markup = more_kb(ll["token"]) if self.conv.more_available() else None
        self._send_cards(chat_id, cards, [html.escape(head)], "", markup)

    def _allowed(self, uid, chat_id, what: str) -> bool:
        """Команда принимается только от владельца и только в личном чате
        с ним (в личном чате chat.id == user.id). В группе даже свои
        сообщения игнорируются — ответ с почтой ушёл бы всем участникам."""
        if uid != self.my_id:
            lg.warning(f"telegram: ЧУЖОЕ {what} от id {uid} — игнорирую")
            return False
        if chat_id != self.my_id:
            lg.warning(f"telegram: {what} от владельца, но не в личном чате "
                       f"(chat {chat_id}) — игнорирую")
            return False
        return True

    def handle_cleanup(self, chat_id, data: str):
        """Очистка корзин по кнопке — детерминированно, без модели: кнопку
        нажал человек, исполняет код с пересчётом. Кнопка действует только
        в день вопроса и один раз в день."""
        action, _, day = data.partition(":")
        today = date.today().isoformat()
        if day != today:
            lg.info(f"telegram: устаревшая кнопка очистки ({data}) — не исполняю")
            self.send(chat_id, "Эта кнопка устарела — вопрос про корзину "
                               "придёт снова вечером.")
            return
        if action == "cleanup_no":
            self.send(chat_id, "Ок, корзины не трогаю — спрошу завтра.")
            return
        if _read_json(CLEANUP_DONE_FILE).get("date") == today:
            lg.info("telegram: очистка сегодня уже выполнена — повтор не исполняю")
            self.send(chat_id, "Корзины сегодня уже чистил — повторно не трогаю.")
            return
        _write_json(CLEANUP_DONE_FILE,
                    {"date": today, "at": datetime.now().isoformat(timespec="seconds")})
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

    def handle(self, update: dict):
        if "message" in update:
            msg = update["message"]
            uid = msg.get("from", {}).get("id")
            chat_id = msg.get("chat", {}).get("id")
            if self.my_id is None:
                if msg.get("chat", {}).get("type") != "private":
                    return
                lg.info(f"telegram: сообщение от id {uid}, TELEGRAM_USER_ID не задан")
                self.send(chat_id,
                          f"Ваш Telegram ID: {uid}\n"
                          f"Впишите в .env строку TELEGRAM_USER_ID={uid} "
                          "и перезапустите бота. До этого я ничего не выполняю.")
                return
            if not self._allowed(uid, chat_id, "сообщение"):
                return
            text = (msg.get("text") or "").strip()
            if not text:
                self.send(chat_id, "Я понимаю только текст.")
                return
            if text == "/start":
                self.send(chat_id, HELP)
                return
            if text == "/new":
                self.conv = Conversation(default_account=self.default_account,
                                         accounts=self.accounts)
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
            if self.my_id is None or not self._allowed(uid, chat_id, "нажатие кнопки"):
                return
            try:  # убрать кнопки, чтобы не нажать дважды
                self.api("editMessageReplyMarkup", http_timeout=15,
                         chat_id=chat_id,
                         message_id=cq.get("message", {}).get("message_id"))
            except RuntimeError:
                pass
            data = cq.get("data") or ""
            if data.startswith("cleanup_"):
                self.handle_cleanup(chat_id, data)
                return
            if data.startswith("more:"):
                self.handle_more(chat_id, data)
                return
            answer = "да" if data == "yes" else "нет"
            self.run_agent(chat_id, answer)

    def _on_term(self, signum, frame):
        """SIGTERM (docker stop / пересборка): если сейчас идёт ответ —
        дать ему завершиться и выйти после него, иначе выйти сразу."""
        self._stop = True
        lg.info("telegram: получен SIGTERM — "
                + ("завершаю текущий ответ" if self._busy else "выхожу"))
        if not self._busy:
            raise SystemExit(0)

    def loop(self):
        # смещение записывается после обработки: если процесс умрёт на
        # середине, Telegram отдаст апдейт снова (повтор безопасен)
        try:
            offset = int(OFFSET_FILE.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            offset = 0
        signal.signal(signal.SIGTERM, self._on_term)
        while not self._stop:
            try:
                updates = self.api("getUpdates", http_timeout=65,
                                   offset=offset, timeout=50)
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                lg.warning(f"telegram: сбой опроса: {e}")
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self._busy = True
                try:
                    self.handle(u)
                except Exception as e:  # noqa: BLE001 — бот не должен падать
                    lg.error(f"telegram: ошибка обработки: {e}")
                finally:
                    self._busy = False
                try:
                    OFFSET_FILE.parent.mkdir(exist_ok=True)
                    OFFSET_FILE.write_text(str(offset), encoding="utf-8")
                except OSError as e:
                    lg.warning(f"telegram: не записал смещение: {e}")
                if self._stop:
                    lg.info("telegram: ответ завершён, выхожу по SIGTERM")
                    return


def main():
    token = config.env_get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ Не задан TELEGRAM_BOT_TOKEN.")
        print("   1) В Telegram: @BotFather → /newbot → скопируйте токен")
        print("   2) cp .env.example .env  и впишите токен")
        print("   Подробности — в SETUP.md, раздел «Этап 4».")
        sys.exit(1)
    uid_raw = config.env_get("TELEGRAM_USER_ID")
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
    print(f" Лог: logs/{agent_log.LOG_FILE.name}  |  остановить: Ctrl+C")
    print("═" * 56)
    lg.info(f"=== старт Telegram-бота @{me.get('username')}, id={my_id}, "
            f"ящик={default_acc} ===")

    def _warm():
        try:
            lg.info(f"прогрев модели: {bot.conv.warmup():.0f} с")
        except Exception as e:  # noqa: BLE001
            lg.warning(f"прогрев модели не удался: {e}")
    threading.Thread(target=_warm, name="warmup", daemon=True).start()
    try:
        bot.loop()
    except KeyboardInterrupt:
        print("\nБот остановлен.")


if __name__ == "__main__":
    main()
