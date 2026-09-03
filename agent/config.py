# -*- coding: utf-8 -*-
"""
Чтение config.yaml без внешних зависимостей.

Это НЕ полный YAML-парсер: он понимает ровно ту плоскую структуру,
которая используется в config.yaml этого проекта:

    секция:
      ключ: "значение"   # комментарий
      ключ: 25

Когда на этапе 4 появится venv, при желании заменим на pyyaml.
"""
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env(path=None) -> dict:
    """Прочитать .env (KEY=VALUE построчно). Файла нет — пустой словарь."""
    p = Path(path) if path else ENV_PATH
    data = {}
    if not p.exists():
        return data
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith('"'):
        end = raw.find('"', 1)
        return raw[1:end] if end > 0 else raw.strip('"')
    # без кавычек: отрезаем комментарий
    raw = raw.split("#", 1)[0].strip()
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def load(path=None) -> dict:
    path = Path(path) if path else CONFIG_PATH
    data, section = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        if not indented and line.rstrip().endswith(":") and ":" in line:
            section = line.strip().rstrip(":")
            data[section] = {}
        elif indented and ":" in line and section:
            key, _, raw = line.strip().partition(":")
            data[section][key.strip()] = _parse_value(raw)
    return data
