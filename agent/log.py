# -*- coding: utf-8 -*-
"""
Логирование агента: logs/agent.log с ротацией (3 файла по 2 МБ).

В лог пишутся все вызовы модели, инструментов и IMAP-команд
с длительностями — по нему разбираем любые тормоза и ошибки.
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_FILE = LOG_DIR / "agent.log"

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
        lg.propagate = False
        _logger = lg
    return _logger
