# -*- coding: utf-8 -*-
"""
Чтение config.yaml и .env без внешних зависимостей.

Это НЕ полный YAML-парсер. Он понимает вложенные словари по отступам,
скаляры в кавычках и без, комментарии после «#»:

    секция:
      ключ: "значение"   # комментарий
      ключ: 25
      вложенная:
        ключ: значение

Списков, многострочных значений и flow-синтаксиса ({a: b}) нет — в этом
проекте они не нужны.

Секреты (пароли ящиков, токен Telegram) живут в .env или в переменных
окружения; переменная окружения имеет приоритет — так их передаёт Docker.
"""
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

DEFAULT_IMAP_PORT = 993


class ConfigError(Exception):
    """Ошибка в config.yaml или .env, понятная пользователю."""


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


def env_get(key: str, default: str = "", env_path=None) -> str:
    """Секрет по имени: сначала переменная окружения, потом .env."""
    value = os.environ.get(key)
    if value is not None and value.strip():
        return value.strip()
    return load_env(env_path).get(key, default)


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith('"'):
        end = raw.find('"', 1)
        return raw[1:end] if end > 0 else raw.strip('"')
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return raw[1:end] if end > 0 else raw.strip("'")
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


def _is_mapping_start(rest: str) -> bool:
    rest = rest.strip()
    if not rest:
        return True
    if rest.startswith(("'", '"')):
        return False
    return not rest.split("#", 1)[0].strip()


def load(path=None) -> dict:
    """config.yaml → вложенные словари."""
    path = Path(path) if path else CONFIG_PATH
    root = {}
    stack = [(-1, root)]  # (отступ, словарь этого уровня)
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if _is_mapping_start(rest):
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_value(rest)
    return root


# ── Ящики ────────────────────────────────────────────────────────────

def accounts(cfg: dict = None) -> list:
    """Ящики из config.yaml (без секретов):
    [{"name", "host", "port", "env", "trash", "drafts"}, ...] в порядке файла."""
    cfg = cfg if cfg is not None else load()
    raw = (cfg.get("mail") or {}).get("accounts") or {}
    if not isinstance(raw, dict):
        raise ConfigError("mail.accounts в config.yaml должен быть словарём ящиков")
    out = []
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"ящик «{name}»: ожидались вложенные поля host и env")
        host = str(spec.get("host") or "").strip()
        env = str(spec.get("env") or "").strip()
        if not host or not env:
            raise ConfigError(f"ящик «{name}»: нужны поля host и env")
        try:
            port = int(spec.get("port") or DEFAULT_IMAP_PORT)
        except (TypeError, ValueError):
            raise ConfigError(f"ящик «{name}»: port должен быть числом")
        out.append({
            "name": str(name).strip(),
            "host": host,
            "port": port,
            "env": env,
            "trash": str(spec.get("trash") or "").strip() or None,
            "drafts": str(spec.get("drafts") or "").strip() or None,
        })
    return out


def credentials(account: dict, env_path=None) -> tuple:
    """(логин, пароль приложения) ящика из <ENV>_USER и <ENV>_PASS.
    Нет хотя бы одного — ConfigError с подсказкой, что вписать в .env."""
    prefix = account["env"]
    user = env_get(f"{prefix}_USER", env_path=env_path)
    password = env_get(f"{prefix}_PASS", env_path=env_path)
    if not user or not password:
        raise ConfigError(
            f"для ящика «{account['name']}» не заданы {prefix}_USER и/или "
            f"{prefix}_PASS — впишите их в .env (см. .env.example)")
    return user, password
