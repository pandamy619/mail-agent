# -*- coding: utf-8 -*-
"""
Состояние одного диалога с агентом и его ход.

Объект держит всё, что раньше было разбросано по модульным переменным:
историю сообщений, заявку на опасное действие, счётчик сообщений
пользователя, последний текст пользователя (его читают гейты
безопасности), карточки писем текущего хода (их рисует интерфейс)
и контекст системного промпта (ящик по умолчанию, список ящиков).

Интерфейс создаёт объект на диалог и зовёт run_turn(); команда «/new»
создаёт новый объект.
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import config, core, llm, mail_index, providers, rules, toolbox
from .log import get as _log
from .tools import mail, mail_actions

DIGEST_FILE = Path(__file__).resolve().parents[1] / "state" / "digest_cards.json"

MAX_STEPS = 8          # защита от зацикливания
MAX_HISTORY = 40       # верхняя граница числа сообщений в контексте
CHARS_PER_TOKEN = 3    # оценка с запасом для русского текста и JSON
ANSWER_RESERVE = 2000  # токенов под результаты инструментов и ответ текущего хода

_CONFIRM_WORDS = {"да", "давай", "давайте", "ага", "ок", "окей", "ok", "yes",
                  "подтверждаю", "выполняй", "удаляй", "переноси",
                  "поехали", "делай", "конечно", "чисти", "очищай",
                  "пробуем", "попробуем", "пробуй", "попробуй",
                  "повтори", "повторяй", "повторить"}


def is_explicit_yes(text: str) -> bool:
    words = re.findall(r"[а-яёa-z]+", (text or "").lower())
    return bool(words) and len(words) <= 3 and all(w in _CONFIRM_WORDS
                                                   for w in words)


_ORDINALS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6, "седьм": 7,
    "восьм": 8, "девят": 9, "десят": 10, "одиннадцат": 11, "двенадцат": 12,
    "тринадцат": 13, "четырнадцат": 14, "пятнадцат": 15, "шестнадцат": 16,
    "семнадцат": 17, "восемнадцат": 18, "девятнадцат": 19, "двадцат": 20,
    "тридцат": 30,
}
_ORD_WORD = re.compile(r"\b(" + "|".join(sorted(_ORDINALS, key=len, reverse=True))
                       + r")(?:ьего|ьей|ье|ью|ое|ый|ая|ую|ого|ой|ому|ым|ые|ых|ь)?\b", re.IGNORECASE)
_ORD_NUM = re.compile(r"(?:№\s*|\b)(\d{1,2})(?:-?(?:е|й|го|ю|ое|ый|ая|ую))?\b")


def list_numbers(text: str) -> list:
    """Номера писем из фразы: «пятнадцатое», «№12», «3-е», «двадцать первое»."""
    out = []
    t = (text or "").lower()
    for m in _ORD_NUM.finditer(t):
        out.append(int(m.group(1)))
    words = [(m.start(), _ORDINALS[m.group(1).lower()]) for m in _ORD_WORD.finditer(t)]
    i = 0
    while i < len(words):
        n = words[i][1]
        if n in (20, 30) and i + 1 < len(words) and words[i + 1][1] < 10 \
                and words[i + 1][0] - words[i][0] < 16:
            n += words[i + 1][1]
            i += 1
        out.append(n)
        i += 1
    seen, res = set(), []
    for n in out:
        if n not in seen:
            seen.add(n); res.append(n)
    return res


def _est_tokens(msg: dict) -> int:
    """Оценка размера сообщения в токенах (вместе с tool_calls)."""
    return len(json.dumps(msg, ensure_ascii=False)) // CHARS_PER_TOKEN + 1


class Conversation:
    def __init__(self, default_account: str = None, accounts: list = None):
        if not default_account and accounts and len(accounts) == 1:
            default_account = accounts[0]["name"]
        self.default_account = default_account
        self.accounts = accounts or []
        self.history = [self.system_message()]
        self.pending = None          # заявка на опасное действие
        self.user_msg_count = 0      # гейт подтверждений: заявка младше счётчика
        self.last_user_text = ""     # его читают гейты remember_rule / mark_read
        self._cards = []             # карточки писем текущего хода
        self.last_list = None        # последний показанный список (для «Ещё 10»)
        self._list_seq = 0
        self._numbered = {}          # n → карточка последнего показанного списка
        self._numbered_at = 0.0      # когда список показан (свежий дайджест старше — не берём)
        self._n_base = 0             # с какого номера нумеровать карточки (после дайджеста)

    # ── системный промпт ────────────────────────────────────────────

    def system_message(self) -> dict:
        if self.default_account:
            rule = (f"- Ящик по умолчанию: {self.default_account}. Когда пользователь "
                    "явно не называет другой ящик, используй его и не переспрашивай.")
        else:
            rule = ("- Если пользователь не назвал ящик, СНАЧАЛА спроси, в каком "
                    "искать, и перечисли варианты из list_accounts.")
        if self.accounts:
            pairs = ", ".join(
                f"{a['name']} ({a['email']})" if a.get("email") and a["email"] != "?"
                else a["name"] for a in self.accounts)
            accounts_line = (f"Ящики пользователя: {pairs}. Называя ящик в ответе, "
                             "указывай его адрес в скобках.")
        else:
            accounts_line = "Точные имена ящиков даёт инструмент list_accounts."
        rb = rules.rules_block()
        rules_section = (f"\nПостоянные правила Влада (соблюдай всегда; при "
                         f"конфликте — более позднее):\n{rb}\n" if rb else "")
        crit = rules.importance_criteria()
        importance_section = (f"\nКритерии важности писем (по ним отвечай, есть ли "
                              f"важное):\n{crit}\n" if crit else "")
        return {"role": "system",
                "content": core.SYSTEM_PROMPT.format(
                    account_rule=rule,
                    accounts_line=accounts_line,
                    rules_section=rules_section,
                    importance_section=importance_section)}

    # ── карточки хода ───────────────────────────────────────────────

    def card(self, m: dict) -> dict:
        """Карточка для модели; параллельно копится полная карточка хода
        со сквозным номером n — интерфейс рисует список сам."""
        mid = int(m["id"])
        acc = m.get("account") or ""
        for c in self._cards:
            if c["id"] == mid and c["account"] == acc:
                n = c["n"]
                break
        else:
            n = self._n_base + len(self._cards) + 1
            self._cards.append({
                "n": n, "id": mid, "account": acc, "sender": m.get("sender", ""),
                "subject": m.get("subject", ""), "received": m.get("received"),
                "age_str": m.get("age_str", ""), "unread": bool(m.get("unread")),
                "category": providers.label(m["category"]) if m.get("category") else "",
            })
        card = {"n": n, "id": mid, "age": m.get("age_str", ""),
                "unread": m.get("unread"), "sender": m.get("sender", ""),
                "subject": m.get("subject", "")}
        if m.get("category"):
            card["category"] = providers.label(m["category"])
        if m.get("folder") and m["folder"] != "INBOX":
            card["folder"] = m["folder"]
            self._cards[n - 1]["category"] = m.get("folder_label") or m["folder"]
            self._cards[n - 1]["folder"] = m["folder"]
        return card

    def fmt_list(self, rows: list, total: int = None) -> str:
        """Результат списка для модели: total — сколько всего (в папке,
        непрочитанных, во Входящих), shown — сколько карточек показано."""
        out = [self.card(m) for m in rows]
        return json.dumps({"total": len(out) if total is None else int(total),
                           "shown": len(out), "messages": out}, ensure_ascii=False)

    # ── листание списков кодом («Ещё 10») ───────────────────────────

    def remember_list(self, args: dict, offset: int, total: int, shown: int) -> None:
        """Запомнить запрос последнего списка, чтобы интерфейс мог показать
        следующую страницу без вызова модели."""
        self._list_seq += 1
        self.last_list = {"args": dict(args), "offset": int(offset),
                          "total": int(total), "shown": int(shown),
                          "token": f"more:{self._list_seq}"}

    def more_available(self) -> bool:
        ll = self.last_list
        return bool(ll) and ll["shown"] > 0 and ll["offset"] + ll["shown"] < ll["total"]

    def page_more(self, token: str) -> list:
        """Следующая страница последнего списка тем же инструментом, без
        модели. Карточки нумеруются дальше; в историю добавляется
        результат инструмента, чтобы модель знала показанное.
        Возвращает новые карточки; [] — если кнопка устарела или всё показано."""
        ll = self.last_list
        if not ll or ll["token"] != token or not self.more_available():
            return []
        before = len(self._cards)
        if ll.get("kind") == "ids":
            return self._page_ids(ll, before)
        args = dict(ll["args"], offset=ll["offset"] + ll["shown"])
        result = toolbox.execute(self, "mail_list", args)
        self.history.append({"role": "assistant", "content": "",
                             "tool_calls": [{"function": {"name": "mail_list",
                                                          "arguments": args}}]})
        self.history.append({"role": "tool", "tool_name": "mail_list", "content": result})
        _log().info(f"листание кодом: mail_list offset={args['offset']} → "
                    f"{len(self._cards) - before} карточек")
        for c in self._cards[before:]:
            self._numbered[c["n"]] = dict(c)
        self._numbered_at = time.time()
        return [dict(c) for c in self._cards[before:]]

    def show_ids(self, items: list, page: int = 10) -> list:
        """Показать список писем по id (кнопка категории под дайджестом):
        первая страница сразу, дальше — «Ещё 10». Нумерация продолжает
        номера дайджеста."""
        self._cards.clear()
        self._n_base = max(self._numbered) if self._numbered else 0
        self._list_seq += 1
        self.last_list = {"kind": "ids", "args": {}, "items": list(items), "offset": 0,
                          "shown": 0, "total": len(items), "page": page,
                          "token": f"more:{self._list_seq}"}
        return self._page_ids(self.last_list, 0)

    def _page_ids(self, ll: dict, before: int) -> list:
        start = ll["offset"] + ll["shown"]
        chunk = ll["items"][start:start + ll.get("page", 10)]
        rows = []
        for acc in {i["account"] for i in chunk}:
            ids = [i["id"] for i in chunk if i["account"] == acc]
            try:
                rows.extend(mail.fetch_by_ids(acc, ids))
            except mail.MailError as e:
                _log().warning(f"show_ids {acc}: {e}")
        order = {(i["account"], i["id"]): k for k, i in enumerate(chunk)}
        rows.sort(key=lambda r: order.get((r["account"], r["id"]), 999))
        result = self.fmt_list(rows, ll["total"])
        self._list_seq += 1
        self.last_list = dict(ll, offset=start, shown=len(chunk), token=f"more:{self._list_seq}")
        self.history.append({"role": "assistant", "content": "",
                             "tool_calls": [{"function": {"name": "mail_list",
                                                          "arguments": {"ids": [i["id"] for i in chunk]}}}]})
        self.history.append({"role": "tool", "tool_name": "mail_list", "content": result})
        for c in self._cards[before:]:
            self._numbered[c["n"]] = dict(c)
        self._numbered_at = time.time()
        return [dict(c) for c in self._cards[before:]]

    def adopt_digest(self, path=None) -> bool:
        """Подхватить номера утреннего дайджеста из файла checker'а, если он
        свежее последнего списка, показанного в этом диалоге."""
        path = Path(path) if path else DIGEST_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            at = float(data.get("at") or 0)
            rows = data.get("rows") or []
        except (OSError, ValueError, TypeError):
            return False
        if not rows or at <= self._numbered_at:
            return False
        self._numbered = {int(r["n"]): dict(r) for r in rows if "n" in r}
        self._numbered_at = at
        _log().info(f"диалог: номера из дайджеста ({len(rows)} строк)")
        return True

    def number_hint(self, text: str) -> str:
        """«письмо №15: id 37289» для номеров из фразы пользователя — модель
        не считает позиции сама (с 20 карточками она промахивалась).
        Для группы дайджеста («GitLab ×7») — все id группы."""
        hints = []
        for n in list_numbers(text)[:3]:
            c = self._numbered.get(n)
            if not c:
                continue
            ids = [i for i in (c.get("ids") or []) if i is not None]
            if len(ids) > 1:
                hints.append(f"письма №{n} ({c.get('sender', '')} ×{len(ids)}): "
                             f"ids {', '.join(str(i) for i in ids[:300])}")
                continue
            loc = f", папка {c['folder']}" if c.get("folder") not in (None, "", "INBOX") else ""
            hints.append(f"письмо №{n}: id {c['id']}{loc}")
        return "; ".join(hints)

    def turn_cards(self) -> list:
        """Карточки писем, показанных за текущий ход, в порядке номеров."""
        return [dict(c) for c in self._cards]

    def turn_categories(self) -> list:
        out = []
        for c in self._cards:
            if c["category"] and c["category"] not in out:
                out.append(c["category"])
        return out

    # ── заявки на опасные действия ──────────────────────────────────
    # Опасный инструмент лишь готовит заявку; выполнить её может только
    # confirm_action, и код требует, чтобы подтверждение было ПЕРВЫМ
    # сообщением пользователя после заявки и явным коротким согласием
    # («да», кнопка [Да]); всё остальное гасит заявку. Модель не может
    # подтвердить сама себе.

    def has_pending(self) -> bool:
        return self.pending is not None

    def cancel_pending(self) -> None:
        self.pending = None

    def make_pending(self, op: str, acc: str, args: dict) -> str:
        lg = _log()
        acc = mail.resolve_account(acc)
        if op in ("trash_filter", "move_filter"):
            snd = (args.get("sender_contains") or "").strip()
            sub = (args.get("subject_contains") or "").strip()
            if not snd and not sub:
                return json.dumps({"error": "нужен хотя бы один фильтр: "
                                            "sender_contains или subject_contains"},
                                  ensure_ascii=False)
            approx = None
            if mail_index.is_ready(acc):
                approx = mail_index.search(sender_contains=snd or None,
                                           subject_contains=sub or None,
                                           account=acc, limit=5)
            parts = []
            if snd:
                parts.append(f"отправитель содержит «{snd}»")
            if sub:
                parts.append(f"тема содержит «{sub}»")
            fdesc = " и ".join(parts)
            target = None
            if op == "move_filter":
                target = mail_actions.resolve_mailbox(acc, args.get("mailbox"))
            count_txt = (f"~{approx['total']} (по индексу)" if approx else
                         "все совпавшие")
            summary = (f"ВСЕ письма ({fdesc}) из {acc} → "
                       + ("корзина" if op == "trash_filter"
                          else f"папка «{target}»")
                       + f"; ожидается {count_txt}")
            self.pending = {"op": op, "account": acc, "ids": [],
                            "snd": snd, "sub": sub, "target": target,
                            "summary": summary, "umsg": self.user_msg_count}
            payload = {"pending": True, "summary": summary,
                       "expected": count_txt,
                       "letters": ([f"{r['sender']} — {r['subject']}"
                                    for r in approx["rows"]] if approx else []),
                       "note": "исполнитель пройдёт по живому ящику и обработает "
                               "все совпавшие на момент выполнения; на большом "
                               "ящике это может занять около минуты"}
        elif op == "empty_folder":
            role = providers.role_of(args.get("folder")) or ""
            if role not in mail_actions.EMPTYABLE:
                return json.dumps({"error": "очищать можно только trash (корзину) "
                                            "или spam (спам)"}, ensure_ascii=False)
            cnt = mail_actions.count_folder(acc, role)
            self.pending = {"op": op, "account": acc, "ids": [], "target": role,
                            "summary": f"БЕЗВОЗВРАТНО очистить папку "
                                       f"«{providers.role_label(role)}» {acc} ({cnt} писем)",
                            "umsg": self.user_msg_count}
            payload = {"pending": True, "summary": self.pending["summary"],
                       "count": cnt}
        else:
            ids = toolbox.ids_list(args)
            if len(ids) > mail_actions.MAX_BATCH:
                return json.dumps({"error": f"не больше {mail_actions.MAX_BATCH} "
                                            f"писем за одну заявку (запрошено "
                                            f"{len(ids)}) — разбей на части"},
                                  ensure_ascii=False)
            known = mail_index.get_by_ids(acc, ids)
            letters = []
            for mid in ids[:8]:
                info = known.get(int(mid))
                letters.append(f"{info['sender']} — {info['subject']}"
                               if info else f"письмо id {mid}")
            target = None
            if op == "move":
                target = mail_actions.resolve_mailbox(acc, args.get("mailbox"))
            summary = (f"{len(ids)} писем из {acc} → корзина" if op == "trash"
                       else f"{len(ids)} писем из {acc} → папка «{target}»")
            self.pending = {"op": op, "account": acc, "ids": ids,
                            "target": target, "summary": summary,
                            "umsg": self.user_msg_count}
            payload = {"pending": True, "summary": summary, "letters": letters}
        payload["instruction"] = ("покажи пользователю сводку и спроси разрешения; "
                                  "после его нового сообщения-согласия вызови "
                                  "confirm_action, при отказе — cancel_action")
        lg.info(f"заявка создана: {self.pending['summary']}")
        return json.dumps(payload, ensure_ascii=False)

    def confirm(self) -> str:
        lg = _log()
        p = self.pending
        if not p:
            return json.dumps({"error": "нет активной заявки — сначала создай её"},
                              ensure_ascii=False)
        if self.user_msg_count <= p["umsg"]:
            lg.warning(f"confirm_action ОТКЛОНЁН кодом (нет нового сообщения "
                       f"пользователя): {p['summary']}")
            return json.dumps({"error": "отказано: выполнить можно только после "
                                        "НОВОГО сообщения пользователя с явным "
                                        "согласием. Спроси его и дождись ответа"},
                              ensure_ascii=False)
        if self.user_msg_count != p["umsg"] + 1:
            lg.warning(f"confirm_action ОТКЛОНЁН кодом (заявка устарела): "
                       f"{p['summary']}")
            self.pending = None
            return json.dumps({"error": "заявка устарела: после неё было несколько "
                                        "сообщений. Создай новую заявку по "
                                        "актуальной просьбе и переспроси"},
                              ensure_ascii=False)
        if not is_explicit_yes(self.last_user_text):
            lg.warning(f"confirm_action ОТКЛОНЁН кодом (сообщение "
                       f"«{self.last_user_text[:60]}» не является явным "
                       f"согласием): {p['summary']}")
            self.pending = None
            return json.dumps({"error": f"отказано кодом: сообщение пользователя "
                                        f"(«{self.last_user_text[:80]}») не является "
                                        "явным согласием — заявка снята. Если это "
                                        "была поправка, создай НОВУЮ заявку по "
                                        "уточнённой просьбе и переспроси; согласие — "
                                        "короткое «да» или кнопка"},
                              ensure_ascii=False)
        self.pending = None
        lg.info(f"заявка подтверждена, выполняю: {p['summary']}")
        if p["op"] == "trash":
            found = mail_actions.trash_by_ids(p["account"], p["ids"])
            return json.dumps({"done": p["summary"], "moved": found,
                               "missing": len(p["ids"]) - found}, ensure_ascii=False)
        if p["op"] == "move":
            found = mail_actions.move_by_ids(p["account"], p["ids"], p["target"])
            return json.dumps({"done": p["summary"], "moved": found,
                               "missing": len(p["ids"]) - found}, ensure_ascii=False)
        if p["op"] in ("trash_filter", "move_filter"):
            if p["op"] == "trash_filter":
                res = mail_actions.trash_by_filter_live(
                    p["account"], p.get("snd") or None, p.get("sub") or None)
            else:
                res = mail_actions.move_by_filter_live(
                    p["account"], p["target"], p.get("snd") or None,
                    p.get("sub") or None)
            left = res["matched"] - res["done"]
            return json.dumps({"done": p["summary"],
                               "found_live": res["matched"], "moved": res["done"],
                               "left": left,
                               "note": ("сообщи числа; если left > 0 — предложи "
                                        "повторить" if left else "")},
                              ensure_ascii=False)
        if p["op"] == "empty_folder":
            res = mail_actions.empty_folder(p["account"], p["target"])
            payload = {"done": p["summary"], "was": res["before"], "left": res["after"]}
            if res["after"] > 0:
                payload["warning"] = (
                    "папка опустела не полностью — сообщи пользователю числа "
                    "и предложи повторить очистку; частая причина — сервер "
                    "ещё синхронизирует папку"
                )
            return json.dumps(payload, ensure_ascii=False)
        return json.dumps({"error": f"неизвестная заявка {p['op']}"},
                          ensure_ascii=False)

    def cancel(self) -> str:
        summary = self.pending["summary"] if self.pending else None
        self.pending = None
        if summary:
            _log().info(f"заявка отменена: {summary}")
        return json.dumps({"cancelled": summary or "заявок не было"},
                          ensure_ascii=False)

    # ── история и ход ───────────────────────────────────────────────

    def history_budget(self) -> int:
        """Сколько токенов истории (без системного промпта и инструментов)
        помещается в окно контекста llm.num_ctx с запасом на ответ."""
        try:
            num_ctx = int(config.load()["llm"].get("num_ctx") or 16384)
        except Exception:  # noqa: BLE001
            num_ctx = 16384
        prefix = (_est_tokens(self.history[0])
                  + len(json.dumps(toolbox.schemas(), ensure_ascii=False)) // CHARS_PER_TOKEN)
        return max(2000, num_ctx - prefix - ANSWER_RESERVE)

    def trimmed(self) -> list:
        """Системный промпт + хвост истории, который влезает в бюджет токенов
        (и не длиннее MAX_HISTORY сообщений). Текущий ход (от последнего
        сообщения пользователя) не режется никогда; хвост не начинается
        с осиротевшего результата инструмента."""
        history = self.history
        tail = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY + 1 else history[1:]
        if not tail:
            return history
        last_user = max((i for i, m in enumerate(tail) if m.get("role") == "user"),
                        default=0)
        budget = self.history_budget()
        total = sum(_est_tokens(m) for m in tail)
        while last_user > 0 and total > budget:
            total -= _est_tokens(tail.pop(0))
            last_user -= 1
        while last_user > 0 and tail[0].get("role") != "user":
            tail.pop(0)
            last_user -= 1
        return [history[0]] + tail

    def warmup(self) -> float:
        """Прогреть кэш промпта Ollama: один вызов с системным промптом
        и инструментами (тот же префикс, что у реальных ходов). Секунды."""
        t0 = time.monotonic()
        llm.chat([self.system_message(), {"role": "user", "content": "привет"}],
                 tools=toolbox.schemas())
        return time.monotonic() - t0

    def run_turn(self, user_text: str, on_tool=None, on_progress=None) -> str:
        """Один ход диалога. on_tool(name, args) — показ вызовов;
        on_progress(text) — живой прогресс долгих операций."""
        lg = _log()
        lg.info(f"user: {user_text}")
        # дата в сообщении, а не в системном промпте: неизменный префикс
        # переиспользуется кэшем Ollama
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        hint = self.number_hint(user_text)
        note = f"(сейчас {stamp}" + (f"; {hint}" if hint else "") + ")"
        self.history.append({"role": "user", "content": f"{user_text}\n{note}"})
        self.user_msg_count += 1
        self.last_user_text = user_text
        try:
            self.history[0] = self.system_message()   # правила перечитываются каждый ход
        except Exception as e:  # noqa: BLE001
            lg.debug(f"rules: не удалось обновить промпт: {e}")
        mail.progress_hook = on_progress
        self._cards.clear()
        self._n_base = 0
        try:
            return self._loop(on_tool)
        finally:
            mail.progress_hook = None
            if self._cards:
                self._numbered = {c["n"]: dict(c) for c in self._cards}
                self._numbered_at = time.time()

    def _loop(self, on_tool=None) -> str:
        lg = _log()
        for _ in range(MAX_STEPS):
            msg = llm.chat(self.trimmed(), tools=toolbox.schemas())
            calls = msg.get("tool_calls") or []
            self.history.append({"role": "assistant",
                                 "content": msg.get("content", ""),
                                 **({"tool_calls": calls} if calls else {})})
            if not calls:
                reply = msg.get("content") or "(модель вернула пустой ответ)"
                lg.info(f"reply: {reply[:300]}")
                return reply
            for tc in calls:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if on_tool:
                    on_tool(name, args)
                t0 = time.monotonic()
                try:
                    result = toolbox.execute(self, name, args)
                    lg.info(f"tool {name} {json.dumps(args, ensure_ascii=False)} → "
                            f"{len(result)} байт за {time.monotonic() - t0:.2f} с")
                except Exception as e:  # noqa: BLE001 — ошибка уходит модели
                    lg.warning(f"tool {name} {json.dumps(args, ensure_ascii=False)} → "
                               f"ERROR {e} за {time.monotonic() - t0:.2f} с")
                    result = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.history.append({"role": "tool", "tool_name": name, "content": result})
        lg.warning("run_turn: превышен лимит шагов")
        return "Не удалось уложиться в 8 шагов — попробуйте переформулировать запрос."
