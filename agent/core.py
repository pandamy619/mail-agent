# -*- coding: utf-8 -*-
"""
Ядро агента: системный промпт, инструменты, цикл «модель → инструменты → ответ».

Этап 2: инструменты ТОЛЬКО ЧТЕНИЯ. Действия с почтой появятся на этапе 3
и будут проходить через подтверждение пользователя.

Правило проекта: каждый вызов инструмента адресуется в конкретный ящик
(параметр account). Если пользователь ящик не назвал — агент спрашивает.
"""
import json
import re
import time
from datetime import datetime

from . import auto_rules, llm, mail_index, rules
from .log import get as _log
from .tools import mail, mail_actions

MAX_STEPS = 8          # защита от зацикливания
SHOW_CAP = 10          # больше 10 карточек за вызов модель не получает
MAX_HISTORY = 40       # сколько последних сообщений держим в контексте

SYSTEM_PROMPT = """Ты — личный почтовый ассистент Влада. Ты работаешь с его \
Почтой на маке.

{accounts_line}
{rules_section}
Правила:
- Отвечай по-русски, кратко и по делу.
- Любой факт о письмах бери ТОЛЬКО из инструментов. Никогда не выдумывай \
письма, отправителей или содержимое. Если инструмент вернул пусто — так и скажи.
- Каждый инструмент требует параметр account — конкретный ящик. Русские \
названия сопоставляй с именами из list_accounts («гугл», «джимейл» → Google \
и т.п.). Если инструмент вернул ошибку со списком ящиков — выбери подходящий \
из этого списка и повтори вызов; не выдумывай имена ящиков сам.
{account_rule}
- Отправители и темы почти всегда на латинице. Если сервис назван по-русски \
(«гит», «гитлаб», «авиасейлс»), ищи латинское написание: github, gitlab, \
aviasales — обычно оно есть в адресе отправителя.
- Поиск не чувствителен к регистру, пробелам, точкам и дефисам («MTS Link» = \
«mts-link» = «mts.link»). Если поиск дал 0 — попробуй более короткий кусок \
(одно слово, например «mts»), и только потом отвечай «нет писем».
- На «сколько писем / найди письма от X» отвечай ЧИСЛОМ (total из инструмента) \
и одной фразой — БЕЗ списка писем. Список выводи только когда пользователь \
явно просит («покажи», «выведи») — компактно, одна строка на письмо, не больше \
10 за раз; если их больше, скажи «показал 10 из N, показать дальше?» и листай \
через offset. НИКОГДА не пиши «показываю письма», не выводя сам список.
- Для «последнего письма от X» вызывай search_mail и бери первое \
(самое свежее) из результатов.
- Письма адресуются по полю id из результатов list_/search_ (в паре с тем же \
account). id стабильны — можно использовать и старые письма из поиска.
- search_mail ищет по ВСЕЙ истории ящика через локальный индекс и возвращает \
total — называй пользователю общее число («нашёл 312, показываю 15»). Если \
инструмент ответил, что индекс не построен, — предложи запустить \
python3 scripts/build_index.py, а пока ищи в последних письмах.
- Чтение и действия со СТАРЫМИ письмами (не из последних ~150) требуют \
поиска по всему ящику — предупреждай, что выполнение может занять минуту-две.
- Действия без подтверждения: mark_read (обратимо) и черновики create_draft / \
reply_draft — письмо только открывается в Почте, отправляет его сам пользователь.
- Опасные действия (trash_messages, move_messages, trash_by_filter, \
move_by_filter, empty_trash) двухфазные: вызов лишь создаёт ЗАЯВКУ и ничего \
не делает. Получив заявку, перескажи пользователю сводку с ТОЧНЫМ числом из \
неё (не выдумывай числа!) и спроси разрешения. confirm_action вызывай ТОЛЬКО \
если следующее сообщение пользователя — явное короткое согласие («да», \
«давай», кнопка) без условий; код это проверяет сам и иначе снимет заявку.
- «Стоп», «нет», любая поправка или изменение объёма — это НЕ согласие: \
немедленно вызови cancel_action и создай НОВУЮ заявку по уточнённой просьбе.
- Для «ВСЕ письма от X» / «все с темой Y» используй trash_by_filter или \
move_by_filter — НЕ собирай ids из показанных результатов: их максимум 10, \
а фильтр охватит все письма истории.
- «Удалить письмо» всегда означает «в корзину», это восстановимо. Безвозвратна \
только очистка корзины (empty_trash); заявка на неё сообщает, сколько всего \
писем в корзине — назови это число пользователю.
- После выполнения заявки (ответа confirm_action) ОБЯЗАТЕЛЬНО отчитайся \
числами из результата: сколько писем обработано (moved/deleted) и сколько не \
нашлось (missing). Если в ответе инструмента error — передай её пользователю \
своими словами. Никогда не отвечай одним словом.
- Если пользователь говорит, что результат в почте не совпадает с твоим \
отчётом, — НЕ выдумывай причин («вы отменили», «произошёл сбой»). Повтори \
ровно то, что вернули инструменты в этом диалоге, и предложи посмотреть \
logs/agent.log; частая причина расхождений — синхронизация Почты с сервером.
- «Запомни: …» → вызови remember_rule с текстом правила (без слова \
«запомни»); подтверди, что именно и под каким номером запомнил. «Какие \
правила?» → list_rules. «Забудь правило N» → forget_rule. Правила вступают \
в силу немедленно.
- Правила со словом «сам»/«автоматически» — АВТО-правила: фон убирает по ним \
письма в корзину утром после дайджеста (без вопросов — корзина обратима), \
а вечером спрашивает про безвозвратную очистку корзины кнопками. На вопрос \
«какие автоматические правила?» — покажи из list_rules только помеченные \
(авто), с интерпретацией и статистикой.
- Отвечай простым текстом без markdown-разметки: никаких **звёздочек**, \
решёток и списков со звёздочками — твой вывод читают в терминале.
- Сегодня: {now}."""

_ACCOUNT_PARAM = {
    "type": "string",
    "description": "точное имя ящика из list_accounts (например Google, iCloud)",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_accounts",
            "description": "Список ящиков (аккаунтов) Почты с точными именами",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent",
            "description": "Последние письма указанного ящика, новые первыми",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "limit": {"type": "integer",
                              "description": "сколько писем показать (максимум 10)"},
                },
                "required": ["account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_unread",
            "description": "Непрочитанные письма среди недавних в указанном ящике",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "limit": {"type": "integer",
                              "description": "сколько показать (по умолчанию 10, максимум 30)"},
                },
                "required": ["account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_mail",
            "description": "Поиск писем по отправителю и/или теме по ВСЕЙ истории "
                           "ящика (локальный индекс). Возвращает total и свежие "
                           "результаты. Нужен хотя бы один фильтр",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "sender_contains": {"type": "string",
                                        "description": "подстрока в отправителе (имя или адрес, латиницей)"},
                    "subject_contains": {"type": "string",
                                         "description": "подстрока в теме"},
                    "limit": {"type": "integer",
                              "description": "сколько карточек вернуть (до 10; по умолчанию 5)"},
                    "offset": {"type": "integer",
                               "description": "сдвиг для листания результатов (по умолчанию 0)"},
                },
                "required": ["account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_mail",
            "description": "Полный текст письма по его id из результатов list_/search_. "
                           "Старое письмо ищется по всему ящику — может занять минуты",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "id": {"type": "integer", "description": "id письма из результатов"},
                },
                "required": ["account", "id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_read",
            "description": "Пометить письма прочитанными (обратимо, без подтверждения)",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "ids": {"type": "array", "items": {"type": "integer"},
                            "description": "id писем из результатов"},
                },
                "required": ["account", "ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mailbox_stats",
            "description": "Сводка по индексу писем: сколько всего, непрочитанных, "
                           "глубина истории по каждому ящику",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_mailboxes",
            "description": "Список папок ящика (для перемещения писем)",
            "parameters": {
                "type": "object",
                "properties": {"account": _ACCOUNT_PARAM},
                "required": ["account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trash_messages",
            "description": "ЗАЯВКА: переместить письма в корзину (по id, работает и "
                           "со старыми письмами из поиска). Ничего не делает сразу — "
                           "выполнится только после согласия пользователя и confirm_action",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "ids": {"type": "array", "items": {"type": "integer"},
                            "description": "id писем из результатов list_/search_"},
                },
                "required": ["account", "ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_messages",
            "description": "ЗАЯВКА: переместить письма (по id) в папку ящика. Выполнится "
                           "только после согласия пользователя и confirm_action",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "ids": {"type": "array", "items": {"type": "integer"}},
                    "mailbox": {"type": "string",
                                "description": "имя папки из list_mailboxes"},
                },
                "required": ["account", "ids", "mailbox"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trash_by_filter",
            "description": "ЗАЯВКА: переместить в корзину ВСЕ письма ящика по "
                           "фильтру, сколько бы их ни было (id берутся из индекса "
                           "целиком). Используй для «все письма от X — в корзину». "
                           "Нужен хотя бы один фильтр. Выполнится после согласия "
                           "и confirm_action",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "sender_contains": {"type": "string",
                                        "description": "подстрока в отправителе (латиницей)"},
                    "subject_contains": {"type": "string",
                                         "description": "подстрока в теме"},
                },
                "required": ["account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_by_filter",
            "description": "ЗАЯВКА: переместить ВСЕ письма ящика по фильтру в папку. "
                           "Выполнится после согласия и confirm_action",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "sender_contains": {"type": "string"},
                    "subject_contains": {"type": "string"},
                    "mailbox": {"type": "string",
                                "description": "имя папки из list_mailboxes"},
                },
                "required": ["account", "mailbox"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "empty_trash",
            "description": "ЗАЯВКА: БЕЗВОЗВРАТНО очистить корзину ящика. Выполнится "
                           "только после согласия пользователя и confirm_action",
            "parameters": {
                "type": "object",
                "properties": {"account": _ACCOUNT_PARAM},
                "required": ["account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_action",
            "description": "Выполнить текущую заявку. Допустимо ТОЛЬКО после нового "
                           "сообщения пользователя с явным согласием",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_action",
            "description": "Отменить текущую заявку (пользователь отказался или передумал)",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_rule",
            "description": "Запомнить постоянное правило (по команде «запомни: …»). "
                           "Действует сразу и во всех интерфейсах",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "текст правила без слова «запомни», до 200 символов"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_rules",
            "description": "Показать постоянные правила с номерами",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_rule",
            "description": "Удалить постоянное правило по номеру из list_rules",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "номер правила"},
                },
                "required": ["n"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_draft",
            "description": "Создать черновик нового письма — оно откроется в Почте, "
                           "отправляет пользователь сам",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "адрес получателя"},
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "текст письма"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_draft",
            "description": "Открыть окно ответа на письмо (по id, с готовым текстом, "
                           "если задан body) — отправляет пользователь сам",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PARAM,
                    "id": {"type": "integer", "description": "id письма из результатов"},
                    "body": {"type": "string", "description": "текст ответа (необязательно)"},
                },
                "required": ["account", "id"],
            },
        },
    },
]


def _fmt_list(rows: list) -> str:
    out = [{
        "id": int(m["id"]), "age": m["age_str"], "unread": m["unread"],
        "sender": m["sender"], "subject": m["subject"],
    } for m in rows]
    return json.dumps({"count": len(out), "messages": out}, ensure_ascii=False)


def _clamp(value, default, max_value=SHOW_CAP) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, max_value))


# ── Заявки на опасные действия ──────────────────────────────────────
# Опасный инструмент лишь готовит заявку; выполнить её может только
# confirm_action, и КОД требует (ужесточено 25.08 после инцидента, когда
# модель приняла «стоп, я имею в виду…» за согласие):
#   1) подтверждение — только ПЕРВЫМ сообщением пользователя после заявки;
#   2) это сообщение — явное короткое согласие («да», «давай», кнопка [Да]);
# всё остальное гасит заявку. Модель не может подтвердить сама себе.

_pending = None
_user_msg_count = 0
_last_user_text = ""

_CONFIRM_WORDS = {"да", "давай", "давайте", "ага", "ок", "окей", "ok", "yes",
                  "подтверждаю", "выполняй", "удаляй", "переноси",
                  "поехали", "делай", "конечно", "чисти", "очищай",
                  "пробуем", "попробуем", "пробуй", "попробуй",
                  "повтори", "повторяй", "повторить"}


def _is_explicit_yes(text: str) -> bool:
    words = re.findall(r"[а-яёa-z]+", (text or "").lower())
    return bool(words) and len(words) <= 3 and all(w in _CONFIRM_WORDS
                                                   for w in words)


def cancel_pending() -> None:
    """Сбросить заявку (например, при /new в CLI)."""
    global _pending
    _pending = None


def has_pending() -> bool:
    """Есть ли сейчас заявка, ждущая подтверждения (для кнопок в интерфейсах)."""
    return _pending is not None


def plain(text: str) -> str:
    """Убрать markdown-разметку из ответа модели — общий для CLI и Telegram."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    return text.replace("`", "")


def _ids_list(args: dict) -> list:
    ids = args.get("ids")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except ValueError:
            ids = None
    if not isinstance(ids, list) or not ids:
        raise mail.MailError("нужен непустой список ids")
    return [int(i) for i in ids]


def _make_pending(op: str, acc: str, args: dict) -> str:
    global _pending
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
        _pending = {"op": op, "account": acc, "ids": [],
                    "snd": snd, "sub": sub, "target": target,
                    "summary": summary, "umsg": _user_msg_count}
        payload = {"pending": True, "summary": summary,
                   "expected": count_txt,
                   "letters": ([f"{r['sender']} — {r['subject']}"
                                for r in approx["rows"]] if approx else []),
                   "note": "исполнитель пройдёт по живому ящику и обработает "
                           "все совпавшие на момент выполнения; на большом "
                           "ящике это займёт минуты — предупреди пользователя"}
        payload["instruction"] = ("покажи пользователю сводку и спроси "
                                  "разрешения; после его нового "
                                  "сообщения-согласия вызови confirm_action, "
                                  "при отказе — cancel_action")
        lg.info(f"заявка создана: {summary}")
        return json.dumps(payload, ensure_ascii=False)
    if op == "empty_trash":
        cnt = mail_actions.count_trash(acc)
        _pending = {"op": op, "account": acc, "ids": [], "target": None,
                    "summary": f"БЕЗВОЗВРАТНО очистить корзину {acc} ({cnt} писем)",
                    "umsg": _user_msg_count}
        payload = {"pending": True, "summary": _pending["summary"],
                   "trash_count": cnt}
    else:
        ids = _ids_list(args)
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
        _pending = {"op": op, "account": acc, "ids": ids,
                    "target": target, "summary": summary,
                    "umsg": _user_msg_count}
        payload = {"pending": True, "summary": summary, "letters": letters,
                   "note": "если письма старые, выполнение может занять "
                           "несколько минут — предупреди пользователя"}
    payload["instruction"] = ("покажи пользователю сводку и спроси разрешения; "
                              "после его нового сообщения-согласия вызови "
                              "confirm_action, при отказе — cancel_action")
    lg.info(f"заявка создана: {_pending['summary']}")
    return json.dumps(payload, ensure_ascii=False)


def _confirm() -> str:
    global _pending
    lg = _log()
    if not _pending:
        return json.dumps({"error": "нет активной заявки — сначала создай её"},
                          ensure_ascii=False)
    if _user_msg_count <= _pending["umsg"]:
        lg.warning(f"confirm_action ОТКЛОНЁН кодом (нет нового сообщения "
                   f"пользователя): {_pending['summary']}")
        return json.dumps({"error": "отказано: выполнить можно только после "
                                    "НОВОГО сообщения пользователя с явным "
                                    "согласием. Спроси его и дождись ответа"},
                          ensure_ascii=False)
    if _user_msg_count != _pending["umsg"] + 1:
        lg.warning(f"confirm_action ОТКЛОНЁН кодом (заявка устарела): "
                   f"{_pending['summary']}")
        _pending = None
        return json.dumps({"error": "заявка устарела: после неё было несколько "
                                    "сообщений. Создай новую заявку по "
                                    "актуальной просьбе и переспроси"},
                          ensure_ascii=False)
    if not _is_explicit_yes(_last_user_text):
        lg.warning(f"confirm_action ОТКЛОНЁН кодом (сообщение «{_last_user_text[:60]}» "
                   f"не является явным согласием): {_pending['summary']}")
        _pending = None
        return json.dumps({"error": f"отказано кодом: сообщение пользователя "
                                    f"(«{_last_user_text[:80]}») не является "
                                    "явным согласием — заявка снята. Если это "
                                    "была поправка, создай НОВУЮ заявку по "
                                    "уточнённой просьбе и переспроси; согласие — "
                                    "короткое «да» или кнопка"},
                          ensure_ascii=False)
    p, _pending = _pending, None
    lg.info(f"заявка подтверждена, выполняю: {p['summary']}")
    if p["op"] == "trash":
        found = mail_actions.trash_by_ids(p["account"], p["ids"])
        return json.dumps({"done": p["summary"], "moved": found,
                           "missing": len(p["ids"]) - found}, ensure_ascii=False)
    if p["op"] == "move":
        found = mail_actions.move_by_ids(p["account"], p["ids"], p["target"])
        return json.dumps({"done": p["summary"], "moved": found,
                           "missing": len(p["ids"]) - found}, ensure_ascii=False)
    if p["op"] == "trash_filter":
        res = mail_actions.trash_by_filter_live(
            p["account"], p.get("snd") or None, p.get("sub") or None)
        left = res["matched"] - res["done"]
        return json.dumps({"done": p["summary"],
                           "found_live": res["matched"], "moved": res["done"],
                           "left": left,
                           "note": ("сообщи числа; если left > 0 — предложи "
                                    "повторить" if left else "")},
                          ensure_ascii=False)
    if p["op"] == "move_filter":
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
    if p["op"] == "empty_trash":
        res = mail_actions.empty_trash(p["account"])
        payload = {"done": p["summary"],
                   "was_in_trash": res["before"],
                   "left_in_trash": res["after"]}
        if res["after"] > 0:
            payload["warning"] = (
                "корзина не опустела — сообщи пользователю числа и причину: "
                "скорее всего, Терминалу не хватает разрешения «Универсальный "
                "доступ» (Настройки → Конфиденциальность и безопасность → "
                "Универсальный доступ → включить Terminal), после чего "
                "повторить очистку"
            )
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps({"error": f"неизвестная заявка {p['op']}"},
                      ensure_ascii=False)


def _cancel() -> str:
    global _pending
    summary = _pending["summary"] if _pending else None
    _pending = None
    if summary:
        _log().info(f"заявка отменена: {summary}")
    return json.dumps({"cancelled": summary or "заявок не было"},
                      ensure_ascii=False)


def execute_tool(name: str, args: dict) -> str:
    """Выполнить инструмент; всегда вернуть строку для роли tool."""
    args = args or {}
    acc = args.get("account")
    if name == "list_accounts":
        return json.dumps({"accounts": mail.accounts_info()}, ensure_ascii=False)
    if name == "list_recent":
        return _fmt_list(mail.list_recent(limit=_clamp(args.get("limit"), 10),
                                          account=acc))
    if name == "list_unread":
        return _fmt_list(mail.list_unread(limit=_clamp(args.get("limit"), 10),
                                          account=acc))
    if name == "search_mail":
        snd = (args.get("sender_contains") or "").strip()
        sub = (args.get("subject_contains") or "").strip()
        if not snd and not sub:
            return json.dumps({"error": "нужен хотя бы один фильтр: sender_contains или subject_contains"},
                              ensure_ascii=False)
        canon = mail.resolve_account(acc)
        try:
            # свежесть: подхватить новые письма ящика в индекс перед поиском
            # (окно 50 покрывает даже плотный день корпоративной почты)
            mail.scan(window=50, account=canon)
        except mail.MailError as e:
            _log().debug(f"search: не удалось освежить индекс: {e}")
        if mail_index.is_ready(canon):
            res = mail_index.search(sender_contains=snd or None,
                                    subject_contains=sub or None,
                                    account=canon,
                                    limit=_clamp(args.get("limit"), 5),
                                    offset=max(0, int(args.get("offset") or 0)))
            out = [{"id": r["id"], "age": r["age_str"], "unread": r["unread"],
                    "sender": r["sender"], "subject": r["subject"]}
                   for r in res["rows"]]
            payload = {"total": res["total"], "shown": len(out),
                       "messages": out}
            if res["total"] == 0:
                payload["hint"] = ("ничего не нашлось — попробуй более "
                                   "короткий кусок имени или адреса (одно "
                                   "слово) или другой ящик, прежде чем "
                                   "отвечать «нет писем»")
            return json.dumps(payload, ensure_ascii=False)
        rows = mail.search(sender_contains=snd or None,
                           subject_contains=sub or None,
                           limit=_clamp(args.get("limit"), 5),
                           account=canon)
        payload = json.loads(_fmt_list(rows))
        payload["note"] = ("индекс не построен — искал только среди последних "
                           "~100 писем; полный поиск по всей истории появится "
                           "после запуска: python3 scripts/build_index.py")
        return json.dumps(payload, ensure_ascii=False)
    if name == "read_mail":
        if "id" not in args:
            return json.dumps({"error": "нужен id письма"}, ensure_ascii=False)
        body = mail.get_body_by_id(int(args["id"]), account=acc, max_chars=1500)
        return json.dumps({"id": int(args["id"]), "body": body.strip()},
                          ensure_ascii=False)
    if name == "mark_read":
        n = mail_actions.mark_read_by_ids(acc, _ids_list(args))
        return json.dumps({"marked_read": n}, ensure_ascii=False)
    if name == "mailbox_stats":
        stats = mail_index.counts()
        if not stats:
            return json.dumps({"error": "индекс пуст — предложи пользователю "
                                        "запустить python3 scripts/build_index.py"},
                              ensure_ascii=False)
        return json.dumps({"index": stats,
                           "note": "данные локального индекса «Входящих»"},
                          ensure_ascii=False)
    if name == "list_mailboxes":
        return json.dumps({"mailboxes": mail_actions.list_mailboxes(acc)},
                          ensure_ascii=False)
    if name == "trash_messages":
        return _make_pending("trash", acc, args)
    if name == "move_messages":
        return _make_pending("move", acc, args)
    if name == "trash_by_filter":
        return _make_pending("trash_filter", acc, args)
    if name == "move_by_filter":
        return _make_pending("move_filter", acc, args)
    if name == "empty_trash":
        return _make_pending("empty_trash", acc, args)
    if name == "confirm_action":
        return _confirm()
    if name == "cancel_action":
        return _cancel()
    if name == "remember_rule":
        # НЕ доверяем пересказу модели: если сообщение пользователя начинается
        # с «запомни», берём текст правила ДОСЛОВНО из него (инцидент 26.08:
        # модель выбросила слово «сам», и авто-правило стало обычным)
        text = (args.get("text") or "").strip()
        m = re.match(r"^\s*запомни(?:\s+правило)?\b[\s:,\-—]*",
                     _last_user_text or "", re.IGNORECASE)
        if m:
            verbatim = _last_user_text[m.end():].strip()
            if verbatim:
                if verbatim != text:
                    _log().info(f"remember_rule: дословный текст пользователя "
                                f"вместо пересказа модели («{text[:60]}»)")
                text = verbatim
        try:
            n, entry = rules.add_rule(text)
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        payload = {"remembered": entry, "number": n,
                   "note": "подтверди пользователю текст и номер"}
        if auto_rules.is_auto(entry):
            try:
                spec = auto_rules.ensure_parsed(entry)
                payload["auto"] = auto_rules.spec_human(spec)
                payload["note"] = ("это АВТО-правило — проговори пользователю "
                                   "интерпретацию из поля auto и что уборка "
                                   "идёт утром после дайджеста")
            except ValueError as e:
                payload["auto_warning"] = (f"как авто-правило не разобралось: "
                                           f"{e} — скажи пользователю")
        return json.dumps(payload, ensure_ascii=False)
    if name == "list_rules":
        block = rules.rules_block()
        payload = {"rules": block or "правил пока нет"}
        try:
            autos = []
            for it in auto_rules.get_interpretations():
                if "spec" in it:
                    stats = it.get("stats") or {}
                    stat_txt = (f"последний раз: {stats['last_run']}, "
                                f"убрано {stats['last_count']}"
                                if stats.get("last_run") else "ещё не срабатывало")
                    autos.append(f"{it['n']} (авто): "
                                 f"{auto_rules.spec_human(it['spec'])} — {stat_txt}")
                else:
                    autos.append(f"{it['n']} (авто): не разобрано — {it.get('error')}")
            if autos:
                payload["auto_rules"] = autos
        except Exception as e:  # noqa: BLE001
            payload["auto_note"] = f"интерпретации недоступны: {e}"
        return json.dumps(payload, ensure_ascii=False)
    if name == "forget_rule":
        try:
            removed = rules.remove_rule(args.get("n", 0))
        except (ValueError, TypeError) as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        return json.dumps({"forgotten": removed,
                           "note": "номера оставшихся правил сдвинулись — "
                                   "при следующем «забудь» сверься с list_rules"},
                          ensure_ascii=False)
    if name == "create_draft":
        result = mail_actions.create_draft(args.get("to", ""),
                                           args.get("subject", ""),
                                           args.get("body", ""))
        return json.dumps({"result": result}, ensure_ascii=False)
    if name == "reply_draft":
        if "id" not in args:
            return json.dumps({"error": "нужен id письма"}, ensure_ascii=False)
        canon = mail.resolve_account(acc)
        mid = int(args["id"])
        pos = mail.locate_ids(canon, [mid])
        if mid not in pos:
            return json.dumps({"error": f"письмо id {mid} не найдено во "
                                        f"«Входящих» {canon}"}, ensure_ascii=False)
        result = mail_actions.reply_draft(canon, pos[mid], args.get("body", ""),
                                          expected_id=mid)
        return json.dumps({"result": result}, ensure_ascii=False)
    return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)


_prompt_ctx = {"default_account": None, "accounts": None}


def _build_system() -> dict:
    default_account = _prompt_ctx["default_account"]
    accounts = _prompt_ctx["accounts"]
    if default_account:
        rule = (f"- Ящик по умолчанию: {default_account}. Когда пользователь явно "
                "не называет другой ящик, используй его и не переспрашивай.")
    else:
        rule = ("- Если пользователь не назвал ящик, СНАЧАЛА спроси, в каком "
                "искать, и перечисли варианты из list_accounts.")
    if accounts:
        pairs = ", ".join(
            f"{a['name']} ({a['email']})" if a.get("email") and a["email"] != "?"
            else a["name"] for a in accounts)
        accounts_line = (f"Ящики пользователя: {pairs}. Называя ящик в ответе, "
                         "указывай его адрес в скобках.")
    else:
        accounts_line = "Точные имена ящиков даёт инструмент list_accounts."
    rb = rules.rules_block()
    rules_section = (f"\nПостоянные правила Влада (соблюдай всегда; при "
                     f"конфликте — более позднее):\n{rb}\n" if rb else "")
    return {"role": "system",
            "content": SYSTEM_PROMPT.format(
                now=datetime.now().strftime("%A, %d.%m.%Y %H:%M"),
                account_rule=rule,
                accounts_line=accounts_line,
                rules_section=rules_section)}


def new_history(default_account: str = None, accounts: list = None) -> list:
    _prompt_ctx["default_account"] = default_account
    _prompt_ctx["accounts"] = accounts
    return [_build_system()]


def _trim(history: list) -> list:
    if len(history) <= MAX_HISTORY + 1:
        return history
    return [history[0]] + history[-MAX_HISTORY:]


def run_turn(history: list, user_text: str, on_tool=None, on_progress=None) -> str:
    """Один ход диалога. Модифицирует history.
    on_tool(name, args) — показ вызовов; on_progress(text) — живой прогресс
    долгих операций (поиск старых писем, массовые действия)."""
    global _user_msg_count, _last_user_text
    lg = _log()
    lg.info(f"user: {user_text}")
    history.append({"role": "user", "content": user_text})
    _user_msg_count += 1  # гейт подтверждений: заявка младше этого счётчика
    _last_user_text = user_text
    try:
        history[0] = _build_system()   # правила перечитываются каждый ход
    except Exception as e:  # noqa: BLE001
        lg.debug(f"rules: не удалось обновить промпт: {e}")
    mail.progress_hook = on_progress
    try:
        return _run_turn_inner(history, on_tool)
    finally:
        mail.progress_hook = None


def _run_turn_inner(history: list, on_tool=None) -> str:
    lg = _log()
    for _ in range(MAX_STEPS):
        msg = llm.chat(_trim(history), tools=TOOLS)
        calls = msg.get("tool_calls") or []
        history.append({"role": "assistant",
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
                result = execute_tool(name, args)
                lg.info(f"tool {name} {json.dumps(args, ensure_ascii=False)} → "
                        f"{len(result)} байт за {time.monotonic() - t0:.2f} с")
            except mail.MailNotAuthorized:
                lg.error(f"tool {name}: нет разрешения Автоматизации")
                raise
            except Exception as e:  # noqa: BLE001 — ошибка уходит модели
                lg.warning(f"tool {name} {json.dumps(args, ensure_ascii=False)} → "
                           f"ERROR {e} за {time.monotonic() - t0:.2f} с")
                result = json.dumps({"error": str(e)}, ensure_ascii=False)
            history.append({"role": "tool", "tool_name": name, "content": result})
    lg.warning("run_turn: превышен лимит шагов")
    return "Не удалось уложиться в 8 шагов — попробуйте переформулировать запрос."
