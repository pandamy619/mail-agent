# -*- coding: utf-8 -*-
"""
Фоновая проверка почты (этап 5): за один запуск run_check()
1. берёт новые письма во всех ящиках по UID-курсору;
2. классифицирует их фоновой моделью по критериям data/importance.md;
3. важное шлёт пушем в Telegram, в тихие часы откладывает до утра;
4. в час дайджеста присылает сводку за период и обнуляет счётчики,
   затем авто-уборка по правилам «сам»; вечером — вопрос про корзину.

Только чтение почты (кроме авто-уборки в корзину). Первый запуск создаёт
базовую линию и ничего не шлёт. Точка входа — scripts/check_mail.py.
"""
import json
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

from . import auto_rules, classifier, config, llm, mail_index, providers, render, telegram
from . import rules as agent_rules
from .conversation import DIGEST_FILE
from .log import get as get_log
from .tools import mail, mail_actions

ROOT = Path(__file__).resolve().parents[1]
lg = get_log()

STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "proactive.json"
LOCK_FILE = STATE_DIR / "lock"
IMPORTANCE_FILE = agent_rules.IMPORTANCE_FILE
NEW_CAP = 500       # новых писем за одну проверку (защита от лавины)


# ── состояние ───────────────────────────────────────────────────────

CARD_KEYS = ("account", "id", "sender", "subject", "received", "category", "reason")
CARDS_CAP = 500     # карточек за период храним в состоянии (для дайджеста)


def _fresh_stats(now):
    return {"new": 0, "per_account": {}, "important": [], "cards": [],
            "since": now.isoformat(timespec="minutes")}


def _slim(row: dict) -> dict:
    return {k: row.get(k, "") for k in CARD_KEYS if row.get(k) not in (None, "")}


def accumulate(stt: dict, new_rows: list, important: list) -> None:
    """Учесть новые письма периода: счётчики, карточки (свежие первыми,
    не больше CARDS_CAP) и важные."""
    stt["new"] = stt.get("new", 0) + len(new_rows)
    per = stt.setdefault("per_account", {})
    for r in new_rows:
        per[r["account"]] = per.get(r["account"], 0) + 1
    cards = [_slim(r) for r in new_rows] + stt.get("cards", [])
    stt["cards"] = cards[:CARDS_CAP]
    stt.setdefault("important", []).extend(_slim(it) for it in important)


def load_state(now):
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            lg.warning("proactive: state повреждён — начинаю заново")
    # last_digest = сегодня: в день первой установки пустой дайджест не шлём,
    # первый придёт следующим утром
    return {"cursor": {}, "pending": [], "last_digest": now.date().isoformat(),
            "stats": _fresh_stats(now)}


def save_state(st):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                          encoding="utf-8")


# ── время ───────────────────────────────────────────────────────────

def _t(s):
    h, m = str(s).strip().split(":")
    return dtime(int(h), int(m))


def parse_quiet(s):
    try:
        a, b = str(s).split("-")
        return (_t(a), _t(b))
    except (ValueError, AttributeError):
        return None


def in_quiet(now_t, rng):
    if not rng:
        return False
    a, b = rng
    if a <= b:
        return a <= now_t < b
    return now_t >= a or now_t < b   # диапазон через полночь


# ── telegram ────────────────────────────────────────────────────────

def tg_send(text: str = "", markup: dict = None, parts: list = None) -> bool:
    """Сообщение владельцу через общий транспорт (agent/telegram.py)."""
    return telegram.notify(text, markup=markup, parts=parts)


def cleanup_kb(day: str) -> dict:
    """Кнопки вечернего вопроса; дата в callback_data — бот исполняет
    очистку только в день вопроса и только один раз."""
    return {"inline_keyboard": [[{"text": "✅ Да, очистить",
                                  "callback_data": f"cleanup_yes:{day}"},
                                 {"text": "❌ Нет",
                                  "callback_data": f"cleanup_no:{day}"}]]}


def run_auto_cleanup(send) -> None:
    """Утренняя авто-уборка: правила «сам» → корзина, сам, с отчётом.
    Живой проход запускается только если по индексу есть кандидаты."""
    try:
        interps = auto_rules.get_interpretations()
    except Exception as e:  # noqa: BLE001
        lg.warning(f"cleanup: интерпретации не получены: {e}")
        return
    lines = []
    for it in interps:
        if "spec" not in it:
            lines.append(f"правило {it['n']}: не разобрано ({it.get('error')})")
            continue
        spec = it["spec"]
        accounts = ([spec["account"]] if spec.get("account")
                    else [a["name"] for a in mail.accounts_info()])
        total = 0
        for acc_name in accounts:
            try:
                acc = mail.resolve_account(acc_name)
            except mail.MailError as e:
                lines.append(f"правило {it['n']}: ящик? {e}")
                continue
            try:
                est = len(mail_index.search_ids(
                    acc, spec.get("sender_contains"),
                    spec.get("subject_contains"),
                    older_days=spec.get("older_days", 0)))
            except Exception:  # noqa: BLE001
                est = 1  # индекс молчит — проверим вживую
            if est == 0:
                continue
            try:
                res = mail_actions.trash_by_filter_live(
                    acc, spec.get("sender_contains"),
                    spec.get("subject_contains"),
                    older_days=spec.get("older_days", 0))
                total += res["done"]
            except mail.MailError as e:
                lines.append(f"правило {it['n']} ({acc}): ошибка {e}")
        auto_rules.record_run(it["text"], total)
        if total:
            lines.append(f"правило {it['n']}: {auto_rules.spec_human(spec)} — "
                         f"убрано {total}")
    if lines:
        send("🧹 Авто-уборка по правилам:\n" + "\n".join(lines))
        lg.info(f"cleanup: {lines}")


def _acc_label(account: str) -> str:
    try:
        em = mail.account_email(account)
        return f"{account} ({em})" if em not in ("", "?") else account
    except mail.MailError:
        return account


def fmt_important(items: list) -> str:
    blocks = []
    for it in items:
        line = f"[{_acc_label(it['account'])}] {it['sender']}\n{it['subject']}"
        if it.get("reason"):
            line += f"\n— {it['reason']}"
        blocks.append(line)
    return "\n\n".join(blocks)


# ── основная логика ─────────────────────────────────────────────────

def collect_new(st) -> list:
    """Новые письма по UID-курсору на ящик. Первый запуск и смена
    UIDVALIDITY — базовая линия без пингов. Старое состояние со списком
    «увиденных» id мигрирует: курсор = максимальный из них."""
    all_new = []
    cursors = st.setdefault("cursor", {})
    legacy = st.pop("seen", None) or {}
    for a in mail.accounts_info():
        acc = a["name"]
        last = cursors.get(acc)
        if last is None and legacy.get(acc):
            last = max(int(i) for i in legacy[acc])
            lg.info(f"proactive: {acc}: миграция состояния — курсор {last}")
        try:
            rows, newest, reset = mail.new_since(acc, last, cap=NEW_CAP)
        except mail.MailError as e:
            lg.warning(f"proactive: {acc}: {e}")
            continue
        if last is None or reset:
            cursors[acc] = newest
            lg.info(f"proactive: {acc}: {'UIDVALIDITY сменился' if reset else 'первый запуск'}"
                    f" — базовая линия (UID {newest}), пинги не шлём")
            continue
        cursors[acc] = max(int(newest), int(last))
        if rows:
            lg.info(f"proactive: {acc}: новых писем {len(rows)}")
        all_new.extend(rows)
    return all_new


def digest_cards(st) -> tuple:
    """(все карточки периода, важные) для рендера: категории — подписями,
    важные — с превью, статус «непрочитано» — по живому ящику."""
    stt = st["stats"]
    cards = [dict(c, category=providers.label(c.get("category", "")))
             for c in stt.get("cards", [])]
    seen, important = set(), []
    for it in stt.get("important", []):
        key = (it.get("account"), it.get("id") or it.get("subject"))
        if key in seen:
            continue
        seen.add(key)
        important.append(dict(it, category=providers.label(it.get("category", ""))))
    unseen = {}
    for acc in {c.get("account") for c in cards + important}:
        try:
            unseen[acc] = mail.unseen_ids(acc)
        except mail.MailError as e:
            lg.debug(f"digest: непрочитанные {acc}: {e}")
    for c in cards + important:
        c["unread"] = c.get("id") in unseen.get(c.get("account"), set())
    try:
        mail.add_previews(important)
    except Exception as e:  # noqa: BLE001
        lg.debug(f"digest: превью не получены: {e}")
    return cards, important


def build_digest(st, now) -> tuple:
    """(HTML-части для Telegram, текст для терминала, строки с номерами)."""
    cards, important = digest_cards(st)
    blocks = render.digest_blocks(now, cards, important)
    return (render.digest_html(now, cards, important),
            render.digest_text(now, cards, important),
            render.digest_numbered(blocks), blocks["count_items"])


def save_digest_rows(rows: list, now=None, count_items: dict = None) -> None:
    """Строки дайджеста с номерами и письма категорий «числом» —
    для бота («покажи третье», кнопки под дайджестом)."""
    from . import providers
    now = now or datetime.now()
    key_of = {v: k for k, v in providers.LABELS.items()}
    groups = {key_of[label]: items for label, items in (count_items or {}).items()
              if label in key_of}
    STATE_DIR.mkdir(exist_ok=True)
    DIGEST_FILE.write_text(json.dumps(
        {"at": now.timestamp(), "day": now.date().isoformat(),
         "rows": rows, "groups": groups}, ensure_ascii=False), encoding="utf-8")


def run_check(now=None, send=tg_send):
    now = now or datetime.now()
    cfg = config.load().get("proactive", {})
    if not cfg.get("enabled", False):
        lg.info("proactive: выключено в конфиге — выхожу")
        return
    STATE_DIR.mkdir(exist_ok=True)
    if LOCK_FILE.exists() and (time.time() - LOCK_FILE.stat().st_mtime) < 600:
        lg.info("proactive: предыдущая проверка ещё идёт — выхожу")
        return
    LOCK_FILE.write_text(now.isoformat(), encoding="utf-8")
    try:
        _check(now, cfg, send)
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def _check(now, cfg, send):
    st = load_state(now)
    quiet = in_quiet(now.time(), parse_quiet(cfg.get("quiet_hours", "")))
    st.setdefault("stats", _fresh_stats(now))

    all_new = collect_new(st)

    important = []
    if all_new:
        criteria = (IMPORTANCE_FILE.read_text(encoding="utf-8")
                    if IMPORTANCE_FILE.exists()
                    else "Важно: письма от живых людей. Рассылки не важны.")
        try:
            rb = agent_rules.rules_block()
            if rb:
                criteria += "\n\nПостоянные правила Влада:\n" + rb
        except Exception as e:  # noqa: BLE001
            lg.debug(f"proactive: правила не подгрузились: {e}")
        try:
            important = classifier.classify(all_new, criteria)
        except llm.LLMError as e:
            lg.warning(f"proactive: классификатор недоступен ({e}) — "
                       "письма попадут в дайджест числом")

    stt = st["stats"]
    accumulate(stt, all_new, important)
    slim = [_slim(it) for it in important]

    if slim:
        if quiet:
            st.setdefault("pending", []).extend(slim)
            lg.info(f"proactive: тихие часы — отложено пингов: {len(slim)}")
        else:
            send("🔔 Важное:\n\n" + fmt_important(slim))
            lg.info(f"proactive: отправлен пуш о {len(slim)} письмах")

    if cfg.get("digest_enabled", False) and not quiet:
        try:
            digest_after = _t(cfg.get("digest_time", "08:00"))
        except ValueError:
            digest_after = dtime(8, 0)
        today = now.date().isoformat()
        if st.get("last_digest", "") != today and now.time() >= digest_after:
            parts, text, numbered, count_items = build_digest(st, now)
            send(text, parts=parts,
                 markup=render.digest_keyboard(count_items, now.date().isoformat()))
            save_digest_rows(numbered, now, count_items)
            lg.info("proactive: отправлен дайджест")
            st["pending"] = []
            st["stats"] = _fresh_stats(now)
            st["last_digest"] = today
            if cfg.get("cleanup_enabled", False):
                run_auto_cleanup(send)

    # вечерний вопрос об очистке корзины (необратимое — всегда с кнопками)
    if cfg.get("cleanup_enabled", False) and not quiet:
        try:
            ask_after = _t(cfg.get("cleanup_report_time", "20:00"))
        except ValueError:
            ask_after = dtime(20, 0)
        today = now.date().isoformat()
        if st.get("last_cleanup_ask", "") != today and now.time() >= ask_after:
            parts = []
            for a in mail.accounts_info():
                try:
                    c = mail_actions.count_trash(a["name"])
                except mail.MailError:
                    continue
                if c > 0:
                    parts.append(f"{a['name']}: {c}")
            if parts:
                send("🗑 Сейчас в корзине — " + ", ".join(parts) + ".\n"
                     "Очистить безвозвратно? Сотрёт корзины целиком, включая "
                     "удалённое вручную.", markup=cleanup_kb(today))
                lg.info(f"cleanup: вечерний вопрос ({parts})")
            st["last_cleanup_ask"] = today

    save_state(st)


