# -*- coding: utf-8 -*-
"""
Реестр инструментов модели: у каждого инструмента схема, обработчик
и его гейты безопасности лежат в одном месте.

    @tool("имя", "описание для модели", params={...}, required=[...])
    def имя(conv, args) -> str: ...

Инструментов немного и они широкие (mail_list вместо четырёх списков,
mail_move вместо четырёх перемещений): так короче промпт и меньше похожих
имён, между которыми модель путается. Порядок регистрации — порядок схем
в промпте (промпт с инструментами кэшируется Ollama байт в байт).
Обработчик получает объект диалога Conversation: гейты читают из него
последнее сообщение пользователя, заявки на опасные действия живут в нём.
"""
import json
import re

from . import auto_rules, mail_index, providers, rules
from .log import get as _log
from .tools import mail, mail_actions

SHOW_CAP = 10          # больше 10 карточек за вызов модель не получает

_SCHEMAS = []
_HANDLERS = {}

# общие описания параметров
ACCOUNT = {"type": "string", "description": "имя ящика (mail_info accounts)"}
IDS = {"type": "array", "items": {"type": "integer"}, "description": "id писем"}
MAIL_ID = {"type": "integer", "description": "id письма"}
FOLDER = {"type": "string",
          "description": "spam, trash, sent, drafts, archive или имя папки"}


def tool(name: str, description: str, params: dict = None, required=()):
    def deco(fn):
        assert name not in _HANDLERS, f"инструмент {name} уже зарегистрирован"
        _SCHEMAS.append({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": dict(params or {}),
                           "required": list(required)}}})
        _HANDLERS[name] = fn
        return fn
    return deco


def schemas() -> list:
    """Схемы инструментов для модели, в порядке регистрации."""
    return list(_SCHEMAS)


def names() -> list:
    return list(_HANDLERS)


def execute(conv, name: str, args: dict) -> str:
    """Выполнить инструмент для диалога conv; всегда вернуть строку для роли tool."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(f"неизвестный инструмент {name}")
    return handler(conv, args or {})


# ── помощники ───────────────────────────────────────────────────────

def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(text: str) -> str:
    return _ok({"error": text})


def clamp(value, default, max_value=SHOW_CAP) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, max_value))


def category(args: dict):
    v = str((args or {}).get("category") or "").strip().lower()
    return v if v in providers.LABELS else None


def ids_list(args: dict) -> list:
    ids = args.get("ids")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except ValueError:
            ids = None
    if not isinstance(ids, list) or not ids:
        raise mail.MailError("нужен непустой список ids")
    return [int(i) for i in ids]


def _text(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "да")


# ── почта: чтение ───────────────────────────────────────────────────

@tool("mail_list",
      "Письма ящика. По умолчанию — последние из Входящих. unread=true — "
      "непрочитанные. sender_contains/subject_contains — поиск по всей истории "
      "Входящих (индекс, возвращает total; листать через offset). folder — "
      "другая папка (spam, trash, sent, drafts, archive или имя). "
      "category — вкладка Gmail, только если пользователь её назвал",
      {"account": ACCOUNT,
       "unread": {"type": "boolean", "description": "только непрочитанные"},
       "sender_contains": {"type": "string", "description": "подстрока в отправителе, латиницей"},
       "subject_contains": {"type": "string", "description": "подстрока в теме"},
       "folder": FOLDER,
       "category": {"type": "string", "description": "promotions, social, updates, primary"},
       "limit": {"type": "integer", "description": "до 10"},
       "offset": {"type": "integer", "description": "сдвиг листания поиска"}},
      ["account"])
def mail_list(conv, args):
    acc = args.get("account")
    limit = clamp(args.get("limit"), 10)
    folder = _text(args, "folder")
    if folder and providers.role_of(folder) != "inbox":
        return conv.fmt_list(mail.list_folder(acc, folder, limit=limit))
    snd, sub = _text(args, "sender_contains"), _text(args, "subject_contains")
    if snd or sub:
        return _search(conv, acc, snd, sub, clamp(args.get("limit"), 5),
                       max(0, int(args.get("offset") or 0)), category(args))
    if _truthy(args.get("unread")):
        return conv.fmt_list(mail.list_unread(limit=limit, account=acc,
                                              category=category(args)))
    rows = mail.list_recent(limit=limit, account=acc)
    if category(args):
        rows = [r for r in rows if r.get("category") == category(args)]
    return conv.fmt_list(rows)


def _search(conv, acc, snd, sub, limit, offset, cat):
    canon = mail.resolve_account(acc)
    try:
        # свежесть: подхватить новые письма ящика в индекс перед поиском
        mail.scan(window=50, account=canon)
    except mail.MailError as e:
        _log().debug(f"search: не удалось освежить индекс: {e}")
    if mail_index.is_ready(canon):
        res = mail_index.search(sender_contains=snd or None, subject_contains=sub or None,
                                account=canon, limit=limit, offset=offset, category=cat)
        out = [conv.card(r) for r in res["rows"]]
        payload = {"total": res["total"], "shown": len(out), "messages": out}
        if res["total"] == 0:
            payload["hint"] = ("ничего не нашлось — попробуй более короткий кусок "
                               "имени или адреса (одно слово) или другой ящик, "
                               "прежде чем отвечать «нет писем»")
        return _ok(payload)
    rows = mail.search(sender_contains=snd or None, subject_contains=sub or None,
                       limit=limit, account=canon)
    if cat:
        rows = [r for r in rows if r.get("category") == cat]
    payload = json.loads(conv.fmt_list(rows))
    payload["note"] = ("индекс не построен — искал только среди последних "
                       "~100 писем; полный поиск по всей истории появится "
                       "после запуска: python3 scripts/build_index.py")
    return _ok(payload)


@tool("mail_read", "Текст письма по id",
      {"account": ACCOUNT, "id": MAIL_ID,
       "folder": {"type": "string",
                  "description": "папка, из которой взят id; для Входящих не указывать"}},
      ["account", "id"])
def mail_read(conv, args):
    if "id" not in args:
        return _err("нужен id письма")
    canon = mail.resolve_account(args.get("account"))
    folder = "INBOX"
    if _text(args, "folder") and providers.role_of(args["folder"]) != "inbox":
        folder = mail.resolve_folder(canon, args["folder"])[0]
    body = mail.get_body_by_id(int(args["id"]), account=canon, max_chars=1500,
                               folder=folder)
    payload = {"id": int(args["id"]), "body": body.strip()}
    if folder == "INBOX":
        info = mail_index.get_by_ids(canon, [int(args["id"])]).get(int(args["id"]))
        if info and info.get("category"):
            payload["category"] = providers.label(info["category"])
    else:
        payload["folder"] = folder
    return _ok(payload)


@tool("mail_info", "Справочник: accounts — ящики с адресами; folders — папки "
      "ящика; stats — сводка индекса (всего, непрочитанных, глубина)",
      {"what": {"type": "string", "description": "accounts, folders или stats"},
       "account": ACCOUNT}, ["what"])
def mail_info(conv, args):
    what = _text(args, "what").lower()
    if what == "accounts":
        return _ok({"accounts": mail.accounts_info()})
    if what == "folders":
        return _ok({"folders": mail_actions.list_mailboxes(args.get("account"))})
    if what == "stats":
        stats = mail_index.counts()
        if not stats:
            return _err("индекс пуст — предложи пользователю запустить "
                        "python3 scripts/build_index.py")
        return _ok({"index": stats, "note": "данные локального индекса «Входящих»"})
    return _err("what должен быть accounts, folders или stats")


# ── почта: действия ─────────────────────────────────────────────────

@tool("mail_mark_read", "Пометить письма прочитанными (только по явной просьбе)",
      {"account": ACCOUNT, "ids": IDS}, ["account", "ids"])
def mail_mark_read(conv, args):
    if not re.search(r"прочит|прочт", conv.last_user_text or "", re.IGNORECASE):
        _log().warning(f"mark_read ОТКЛОНЁН кодом: пользователь не просил "
                       f"(«{(conv.last_user_text or '')[:60]}»)")
        return _err("отказано кодом: помечать прочитанным можно только по явной "
                    "просьбе пользователя («пометь прочитанным»)")
    n = mail_actions.mark_read_by_ids(args.get("account"), ids_list(args))
    return _ok({"marked_read": n})


@tool("mail_move",
      "ЗАЯВКА: переместить письма Входящих в корзину (target=trash) или папку. "
      "Либо ids из результатов, либо фильтр sender_contains/subject_contains — "
      "тогда ВСЕ совпавшие письма истории (для «все письма от X»). Ничего не "
      "делает сразу: выполнится после согласия пользователя и confirm_action",
      {"account": ACCOUNT,
       "target": {"type": "string", "description": "trash или имя папки"},
       "ids": IDS,
       "sender_contains": {"type": "string", "description": "подстрока в отправителе, латиницей"},
       "subject_contains": {"type": "string", "description": "подстрока в теме"}},
      ["account", "target"])
def mail_move(conv, args):
    target = _text(args, "target")
    if not target:
        return _err("нужен target: trash или имя папки")
    to_trash = providers.role_of(target) == "trash"
    by_filter = bool(_text(args, "sender_contains") or _text(args, "subject_contains"))
    if by_filter:
        op = "trash_filter" if to_trash else "move_filter"
    else:
        if not args.get("ids"):
            return _err("нужны ids писем или фильтр sender_contains/subject_contains")
        op = "trash" if to_trash else "move"
    if not to_trash:
        args = dict(args, mailbox=target)
    return conv.make_pending(op, args.get("account"), args)


@tool("mail_empty",
      "ЗАЯВКА: БЕЗВОЗВРАТНО очистить корзину (trash) или спам (spam) ящика; "
      "выполнится после согласия и confirm_action",
      {"account": ACCOUNT, "folder": {"type": "string", "description": "trash или spam"}},
      ["account", "folder"])
def mail_empty(conv, args):
    return conv.make_pending("empty_folder", args.get("account"), args)


@tool("confirm_action",
      "Выполнить текущую заявку — только после нового сообщения пользователя "
      "с явным согласием")
def confirm_action(conv, args):
    return conv.confirm()


@tool("cancel_action", "Отменить текущую заявку")
def cancel_action(conv, args):
    return conv.cancel()


# ── правила ─────────────────────────────────────────────────────────

@tool("rules", "Постоянные правила: action=list — показать с номерами; "
      "add — запомнить (по команде «запомни: …»); forget — удалить по номеру n",
      {"action": {"type": "string", "description": "list, add или forget"},
       "text": {"type": "string", "description": "текст правила для add"},
       "n": {"type": "integer", "description": "номер правила для forget"}},
      ["action"])
def rules_tool(conv, args):
    action = _text(args, "action").lower()
    if action == "add":
        return _rules_add(conv, args)
    if action == "forget":
        try:
            removed = rules.remove_rule(args.get("n", 0))
        except (ValueError, TypeError) as e:
            return _err(str(e))
        return _ok({"forgotten": removed,
                    "note": "номера оставшихся правил сдвинулись — "
                            "при следующем «забудь» сверься со списком"})
    if action == "list":
        return _rules_list()
    return _err("action должен быть list, add или forget")


def _rules_add(conv, args):
    # правило берётся дословно из сообщения пользователя, а не из пересказа
    # модели: иначе текст чужого письма мог бы стать авто-правилом «удаляй сам»
    m = re.match(r"^\s*запомни(?:\s+правило)?\b[\s:,\-—]*",
                 conv.last_user_text or "", re.IGNORECASE)
    verbatim = conv.last_user_text[m.end():].strip() if m else ""
    if not verbatim:
        _log().warning(f"rules add ОТКЛОНЁН кодом: сообщение пользователя "
                       f"«{(conv.last_user_text or '')[:60]}» не начинается с «запомни»")
        return _err("отказано кодом: правила добавляются только командой "
                    "пользователя, которая начинается со слова «запомни». "
                    "Попроси его написать: «запомни: …»")
    text = verbatim
    if text != _text(args, "text"):
        _log().info(f"rules add: дословный текст пользователя вместо "
                    f"пересказа модели («{_text(args, 'text')[:60]}»)")
    try:
        n, entry = rules.add_rule(text)
    except ValueError as e:
        return _err(str(e))
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
    return _ok(payload)


def _rules_list():
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
    return _ok(payload)


# ── черновики ───────────────────────────────────────────────────────

@tool("draft", "Черновик в папке «Черновики» ящика, отправляет пользователь сам. "
      "reply_to_id — ответ на письмо с цитатой; иначе новое письмо (нужны to и subject)",
      {"account": ACCOUNT, "body": {"type": "string", "description": "текст"},
       "reply_to_id": {"type": "integer", "description": "id письма, на которое отвечаем"},
       "to": {"type": "string", "description": "адрес получателя нового письма"},
       "subject": {"type": "string", "description": "тема нового письма"}},
      ["account", "body"])
def draft(conv, args):
    canon = mail.resolve_account(args.get("account"))
    if args.get("reply_to_id"):
        result = mail_actions.reply_draft(canon, int(args["reply_to_id"]),
                                          args.get("body", ""))
    else:
        if not _text(args, "to"):
            return _err("для нового письма нужен адрес to (или reply_to_id для ответа)")
        result = mail_actions.create_draft(canon, args.get("to", ""),
                                           args.get("subject", ""), args.get("body", ""))
    return _ok({"result": result})
