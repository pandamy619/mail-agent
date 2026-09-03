# -*- coding: utf-8 -*-
"""
Постоянные правила агента (этап 6a).

Хранилище — rules.md в корне проекта: одна строка «- …» = одно правило,
с датой добавления. Правила вклеиваются в системный промпт ПЕРЕД КАЖДЫМ
ходом диалога (вступают в силу немедленно) и в критерии классификатора
важности при каждой фоновой проверке.

Ограничения, чтобы промпт не распухал: не больше 50 правил по 200 символов.
"""
from datetime import date
from pathlib import Path

from .log import get as _log

RULES_FILE = Path(__file__).resolve().parents[1] / "rules.md"
MAX_RULES = 50
MAX_LEN = 200

_HEADER = """# Правила агента

Память агента. Команды в чате: «запомни: …», «какие правила?»,
«забудь правило N». Файл можно править и руками: одна строка — одно
правило, начинается с «- ». Прочие строки при изменениях через агента
не сохраняются.

"""


def load_rules() -> list:
    """Список правил (без маркера «- »), в порядке файла."""
    if not RULES_FILE.exists():
        return []
    out = []
    for line in RULES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- ") and line[2:].strip():
            out.append(line[2:].strip())
    return out


def _write(rules: list) -> None:
    body = "".join(f"- {r}\n" for r in rules)
    RULES_FILE.write_text(_HEADER + body, encoding="utf-8")


def rules_block() -> str:
    """Нумерованный блок для промпта; пустая строка, если правил нет."""
    rules = load_rules()
    if not rules:
        return ""
    return "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))


def add_rule(text: str):
    """Добавить правило. Возвращает (номер, записанный текст)."""
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("пустое правило")
    if len(text) > MAX_LEN:
        raise ValueError(f"правило длиннее {MAX_LEN} символов — сформулируй короче")
    rules = load_rules()
    if len(rules) >= MAX_RULES:
        raise ValueError(f"уже {MAX_RULES} правил — сначала «забудь правило N»")
    stamp = date.today().strftime("%d.%m.%Y")
    entry = f"{text} ({stamp})"
    rules.append(entry)
    _write(rules)
    _log().info(f"rules: добавлено №{len(rules)}: {text}")
    return len(rules), entry


def remove_rule(n) -> str:
    """Удалить правило по номеру из «какие правила?». Возвращает его текст."""
    rules = load_rules()
    n = int(n)
    if not 1 <= n <= len(rules):
        raise ValueError(f"нет правила №{n}; сейчас правил: {len(rules)}")
    removed = rules.pop(n - 1)
    _write(rules)
    _log().info(f"rules: удалено №{n}: {removed}")
    return removed
