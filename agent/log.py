# -*- coding: utf-8 -*-
"""
Логирование агента: logs/agent.log с ротацией (3 файла по 2 МБ).

В лог пишутся все вызовы модели, инструментов и IMAP-команд
с длительностями — по нему разбираем любые тормоза и ошибки.

LOG_STDOUT=1 в окружении — тот же лог дублируется в stdout
(в Docker его показывают docker logs и Portainer).
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
# LOG_FILE_NAME — отдельный файл на процесс (в Docker бот и проверка пишут
# в разные файлы: два процесса на одном RotatingFileHandler теряют строки
# при ротации)
LOG_FILE = LOG_DIR / (os.environ.get("LOG_FILE_NAME", "").strip() or "agent.log")

_logger = None


def get() -> logging.Logger:
    global _logger
    if _logger is None:
        LOG_DIR.mkdir(exist_ok=True)
        lg = logging.getLogger("mailagent")
        lg.setLevel(logging.DEBUG)
        if not lg.handlers:
            h = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000,
                                    backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(message)s"))
            lg.addHandler(h)
            if os.environ.get("LOG_STDOUT", "").strip().lower() not in ("", "0", "false", "no"):
                sh = logging.StreamHandler(sys.stdout)
                sh.setFormatter(logging.Formatter(
                    "%(asctime)s | %(levelname)-7s | %(message)s"))
                lg.addHandler(sh)
        lg.propagate = False
        _logger = lg
    return _logger
