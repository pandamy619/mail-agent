#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Этап 0 — проверка фундамента почтового агента.

Проверяет три вещи:
  1) Ollama запущена и отвечает;
  2) нужная модель скачана;
  3) модель умеет tool calling — вызывает инструмент и использует его результат.

Зависимостей нет — только стандартная библиотека Python 3.

Запуск из папки проекта:
    python3 scripts/test_llm.py              # модель по умолчанию: qwen3:14b
    python3 scripts/test_llm.py qwen2.5:14b  # проверить другую модель
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:11434"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3:14b"


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def post(path, payload, timeout=600):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fail(msg):
    print("\n❌ " + msg)
    print("\nПришлите весь вывод этого скрипта Клоду — разберёмся.")
    sys.exit(1)


print(f"— Проверяю фундамент (модель: {MODEL}) —\n")

# ── 1. Ollama запущена? ──────────────────────────────────────────────
try:
    v = get("/api/version")
    print(f"✅ Ollama запущена (версия {v.get('version', '?')})")
except (urllib.error.URLError, OSError):
    fail(
        "Ollama не отвечает на localhost:11434.\n"
        "   Запустите приложение Ollama (иконка ламы в строке меню)\n"
        "   или выполните в отдельной вкладке терминала: ollama serve"
    )

# ── 2. Модель скачана? ───────────────────────────────────────────────
names = [m.get("name", "") for m in get("/api/tags").get("models", [])]
if not any(n == MODEL or n.startswith(MODEL) for n in names):
    print(f"   Сейчас скачаны: {', '.join(names) if names else 'ничего'}")
    fail(f"Модель {MODEL} не найдена. Скачайте её командой:  ollama pull {MODEL}")
print(f"✅ Модель {MODEL} на месте")

# ── 3. Tool calling ──────────────────────────────────────────────────
tools = [{
    "type": "function",
    "function": {
        "name": "sum_numbers",
        "description": "Складывает два числа и возвращает сумму",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "первое число"},
                "b": {"type": "number", "description": "второе число"},
            },
            "required": ["a", "b"],
        },
    },
}]

messages = [{
    "role": "user",
    "content": "Сколько будет 379 плюс 264? Обязательно посчитай инструментом sum_numbers, не считай в уме.",
}]

print("⏳ Спрашиваю модель… (первый запрос может занять минуту-две — модель грузится в память)")
resp = post("/api/chat", {"model": MODEL, "messages": messages, "tools": tools, "stream": False})
msg = resp.get("message", {})
calls = msg.get("tool_calls") or []

if not calls:
    print("   Ответ модели вместо вызова инструмента:")
    print("   " + (msg.get("content") or "")[:400])
    fail("Модель НЕ вызвала инструмент — с этой моделью агента не построить, подберём другую.")

fn = calls[0].get("function", {})
args = fn.get("arguments", {})
if isinstance(args, str):
    args = json.loads(args)
a = float(args.get("a", 0))
b = float(args.get("b", 0))
print(f"✅ Модель вызвала {fn.get('name')}(a={a:g}, b={b:g})")
if {a, b} != {379.0, 264.0}:
    print("⚠️  Аргументы отличаются от 379 и 264 — покажите вывод Клоду.")

# отдаём модели результат инструмента и просим финальный ответ
messages.append(msg)
messages.append({"role": "tool", "tool_name": "sum_numbers", "content": str(a + b)})
resp2 = post("/api/chat", {"model": MODEL, "messages": messages, "tools": tools, "stream": False})
final = (resp2.get("message", {}).get("content") or "").strip()
print(f"✅ Финальный ответ модели: {final[:200]}")

if "643" in final:
    print("\n🎉 ЭТАП 0 ПРОЙДЕН: Ollama + модель + tool calling работают.")
    print("Пришлите этот вывод Клоду — переходим к этапу 1 (чтение почты).")
else:
    print("\n⚠️  Инструмент вызван, но в финальном ответе не видно 643.")
    print("Пришлите весь вывод Клоду — посмотрим вместе.")
