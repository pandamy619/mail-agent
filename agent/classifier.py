# -*- coding: utf-8 -*-
"""
Классификатор важности писем (этап 5).

Пачка новых писем уходит в локальную модель ОДНИМ вызовом вместе с
критериями из importance.md; обратно — только номера важных с причинами.
Никаких инструментов у модели здесь нет — чистая сортировка текста.
Модель — proactive.model из config.yaml (отдельная от чата, чтобы не
вытеснять кэш промпта бота).
"""
import json
import re

from . import llm
from .log import get as _log


def classify(letters: list, criteria: str) -> list:
    """letters: [{idx, id, account, sender, subject, age_str}, ...]
    Возвращает только важные — те же словари с добавленным "reason"."""
    if not letters:
        return []
    numbered = "\n".join(
        f"{i + 1}. [{L['account']}] {L['sender']} — {L['subject']}"
        for i, L in enumerate(letters))
    system = (
        "Ты сортируешь входящую почту Влада на важное и неважное.\n"
        f"Критерии Влада:\n{criteria.strip()}\n\n"
        "Тебе дают нумерованный список новых писем (ящик, отправитель, тема).\n"
        "Ответь ТОЛЬКО JSON-массивом важных писем, без пояснений вокруг:\n"
        '[{"n": 1, "reason": "почему важно, кратко"}]\n'
        "Если важных нет — ответь []."
    )
    msg = llm.chat([{"role": "system", "content": system},
                    {"role": "user", "content": numbered}],
                   model=llm.proactive_model(), think=llm.proactive_think(),
                   num_gpu=llm.proactive_num_gpu())
    text = msg.get("content", "")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        _log().warning(f"classifier: не нашёл JSON в ответе: {text[:200]}")
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        _log().warning(f"classifier: битый JSON: {m.group(0)[:200]}")
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(letters):
            row = dict(letters[n - 1])
            row["reason"] = str(item.get("reason", ""))[:200]
            out.append(row)
    _log().info(f"classifier: {len(letters)} новых → важных {len(out)}")
    return out
