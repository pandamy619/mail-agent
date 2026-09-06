# -*- coding: utf-8 -*-
"""Фабрика провайдеров: по IMAP-хосту ящика выбирается класс с особенностями
сервиса (сейчас — категории Gmail); остальные ящики получают базовый IMAP."""
from .base import LABELS, Provider, label  # noqa: F401
from .gmail import GmailProvider

_BY_HOST = {
    "imap.gmail.com": GmailProvider,
    "imap.googlemail.com": GmailProvider,
}

_instances = {}


def for_account(account: dict) -> Provider:
    """Провайдер ящика по его описанию из config.accounts() (кэш по имени)."""
    name = account["name"]
    p = _instances.get(name)
    if p is None:
        cls = _BY_HOST.get(str(account.get("host", "")).lower(), Provider)
        p = cls(account)
        _instances[name] = p
    return p
