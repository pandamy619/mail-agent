# -*- coding: utf-8 -*-
"""
Авто-правила (этап 6b): правила из rules.md со словом «сам»/«автоматически».

Каждое авто-правило один раз разбирается моделью в структуру
{sender_contains?, subject_contains?, older_days, account?} — она
сохраняется в state/auto_rules.json вместе со статистикой срабатываний,
и дальше утреннюю уборку строит КОД, детерминированно. Если правило
изменили руками, оно переразбирается при следующем обращении.

Действие первой версии — только «в корзину» (обратимо).
"""
import json
import re
import time
from pathlib import Path

from . import llm, rules
from .log import get as _log
from .tools import mail as _mail

STORE = Path(__file__).resolve().parents[1] / "state" / "auto_rules.json"

_AUTO_RE = re.compile(r"\bсам\b|\bсама\b|автоматическ", re.IGNORECASE)


def is_auto(text: str) -> bool:
    return bool(_AUTO_RE.search(text or ""))


def _load_store() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text(encoding="utf-8"))
        except ValueError:
            _log().warning("auto_rules: битый store — начинаю заново")
    return {}


def _save_store(data: dict) -> None:
    STORE.parent.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")


def parse_rule(text: str) -> dict:
    """Один вызов модели: текст правила → структура фильтра."""
    system = (
        "Разбери правило почтового ассистента в JSON. Верни ТОЛЬКО JSON вида\n"
        '{"sender_contains": "...", "subject_contains": "...", '
        '"older_days": N, "account": "..."}\n'
        "Поля, которых нет в правиле, опусти. older_days — число из «старше "
        "N дней» (если срока нет — 0). account — имя ящика, ТОЛЬКО если оно "
        "явно названо (Exchange, Google и т.п.), иначе опусти. ВАЖНО: слова "
        "«сам», «сама», «автоматически» — служебные пометки правила, это НЕ "
        "имя ящика. Отправителя пиши латиницей, как он встречается в адресах "
        "(гитлаб → gitlab)."
    )
    msg = llm.chat([{"role": "system", "content": system},
                    {"role": "user", "content": text}],
                   model=llm.proactive_model(), think=llm.proactive_think(),
                   num_gpu=llm.proactive_num_gpu())
    m = re.search(r"\{.*\}", msg.get("content", ""), re.S)
    if not m:
        raise ValueError("модель не смогла разобрать правило — сформулируй "
                         "с отправителем/темой и сроком")
    try:
        raw = json.loads(m.group(0))
    except ValueError:
        raise ValueError("модель вернула битый разбор — переформулируй правило")
    spec = {}
    snd = str(raw.get("sender_contains") or "").strip()
    sub = str(raw.get("subject_contains") or "").strip()
    if snd:
        spec["sender_contains"] = snd
    if sub:
        spec["subject_contains"] = sub
    if not spec:
        raise ValueError("в правиле не видно ни отправителя, ни темы — "
                         "уточни, чьи письма убирать")
    try:
        spec["older_days"] = max(0, int(raw.get("older_days") or 0))
    except (TypeError, ValueError):
        spec["older_days"] = 0
    acc = str(raw.get("account") or "").strip()
    if acc:
        spec["account"] = acc
    spec["action"] = "trash"
    return _validate_account(spec)


def _validate_account(spec: dict) -> dict:
    """account в интерпретации обязан резолвиться в реальный ящик — иначе
    отбрасывается (правило действует по всем). Урок 26.08: разборщик записал
    слово «сам» как ящик "sam". При временной недоступности Почты поле
    не трогаем."""
    acc = spec.get("account")
    if not acc:
        return spec
    try:
        spec["account"] = _mail.resolve_account(acc)
    except _mail.MailError as e:
        if "не найден" in str(e):
            _log().info(f"auto_rules: «{acc}» — не ящик, правило по всем ящикам")
            spec.pop("account", None)
        # иначе Почта просто недоступна — оставляем как есть
    return spec


def ensure_parsed(entry: str) -> dict:
    """Интерпретация правила (из кэша или свежим разбором).
    Кэшированные интерпретации перепроверяются валидацией ящика —
    старые записи с мнимым ящиком самоисправляются."""
    store = _load_store()
    item = store.get(entry)
    if item and "spec" in item:
        spec = _validate_account(dict(item["spec"]))
        if spec != item["spec"]:
            item["spec"] = spec
            _save_store(store)
        return spec
    spec = parse_rule(entry)
    store[entry] = {"spec": spec, "parsed_at": time.strftime("%d.%m.%Y %H:%M"),
                    "stats": item.get("stats", {}) if item else {}}
    _save_store(store)
    _log().info(f"auto_rules: разобрано «{entry}» → {spec}")
    return spec


def get_interpretations() -> list:
    """Все текущие авто-правила с интерпретациями и статистикой.
    Убирает из хранилища интерпретации удалённых правил."""
    all_rules = rules.load_rules()
    store = _load_store()
    out, changed = [], False
    for k in list(store.keys()):
        if k not in all_rules:
            del store[k]
            changed = True
    for i, entry in enumerate(all_rules, 1):
        if not is_auto(entry):
            continue
        try:
            spec = ensure_parsed(entry)
        except ValueError as e:
            out.append({"n": i, "text": entry, "error": str(e)})
            continue
        store = _load_store()
        out.append({"n": i, "text": entry, "spec": spec,
                    "stats": store.get(entry, {}).get("stats", {})})
    if changed:
        _save_store(store)
    return out


def record_run(entry: str, count: int) -> None:
    store = _load_store()
    item = store.setdefault(entry, {})
    item["stats"] = {"last_run": time.strftime("%d.%m %H:%M"),
                     "last_count": int(count)}
    _save_store(store)


def spec_human(spec: dict) -> str:
    parts = []
    if spec.get("sender_contains"):
        parts.append(f"отправитель содержит «{spec['sender_contains']}»")
    if spec.get("subject_contains"):
        parts.append(f"тема содержит «{spec['subject_contains']}»")
    if spec.get("older_days"):
        parts.append(f"старше {spec['older_days']} дн")
    parts.append(spec.get("account") or "все ящики")
    return " · ".join(parts) + " → корзина"
