# -*- coding: utf-8 -*-
"""
Базовый провайдер ящика: всё, что отличает один почтовый сервис от другого
поверх общего IMAP. Обычный IMAP-сервер категорий не знает — базовый класс
возвращает пустые ответы, наследники (Gmail) переопределяют.
"""

# ключ категории → подпись для пользователя
LABELS = {
    "primary": "Несортированные",
    "promotions": "Промоакции",
    "social": "Соцсети",
    "updates": "Оповещения",
    "forums": "Форумы",
}


def label(key: str) -> str:
    return LABELS.get(key or "", key or "")


class Provider:
    name = "imap"
    has_categories = False

    def __init__(self, account: dict):
        self.account = account

    def categories(self, sess, uids: list, folder: str = "INBOX") -> dict:
        """{uid: ключ категории} для писем папки; без категорий — пусто."""
        return {}
