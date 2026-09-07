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


# роль папки → флаг SPECIAL-USE (RFC 6154), подпись, слова пользователя
ROLE_FLAGS = {"inbox": None, "spam": "\\junk", "trash": "\\trash", "sent": "\\sent",
              "drafts": "\\drafts", "archive": "\\archive", "all": "\\all"}
ROLE_LABELS = {"inbox": "Входящие", "spam": "Спам", "trash": "Корзина",
               "sent": "Отправленные", "drafts": "Черновики", "archive": "Архив",
               "all": "Вся почта"}
ROLE_WORDS = {"входящие": "inbox", "inbox": "inbox", "спам": "spam", "spam": "spam",
              "junk": "spam", "корзина": "trash", "удалённые": "trash", "удаленные": "trash",
              "trash": "trash", "отправленные": "sent", "sent": "sent",
              "черновики": "drafts", "drafts": "drafts", "архив": "archive",
              "archive": "archive", "вся почта": "all", "all": "all"}
# запасные имена, когда сервер не отдаёт флаги
FALLBACK_NAMES = {
    "spam": ("Junk", "Spam", "Спам", "Junk E-mail", "INBOX.Junk", "INBOX.Spam"),
    "trash": ("Trash", "Deleted Messages", "Deleted Items", "Корзина", "Удалённые",
              "INBOX.Trash"),
    "sent": ("Sent", "Sent Messages", "Sent Items", "Отправленные", "INBOX.Sent"),
    "drafts": ("Drafts", "Черновики", "INBOX.Drafts"),
    "archive": ("Archive", "Архив", "INBOX.Archive"),
}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def role_of(word: str):
    """Слово пользователя или ключ роли → роль папки, иначе None."""
    return ROLE_WORDS.get((word or "").strip().lower())


class Provider:
    name = "imap"
    has_categories = False
    fallback_names = FALLBACK_NAMES

    def __init__(self, account: dict):
        self.account = account

    def folder(self, sess, role: str) -> str:
        """Имя папки по роли: переопределение из config.yaml, флаг сервера,
        запасные имена сервиса. Нет папки — MailError от вызывающего."""
        if role == "inbox":
            return "INBOX"
        override = str(self.account.get(role) or "").strip()
        if override:
            return override
        flag = ROLE_FLAGS.get(role)
        folders = sess.folders()
        if flag:
            for f in folders:
                if flag in f["flags"]:
                    return f["name"]
        names = {f["name"].lower(): f["name"] for f in folders}
        for cand in self.fallback_names.get(role, ()):
            if cand.lower() in names:
                return names[cand.lower()]
        return ""

    def categories(self, sess, uids: list, folder: str = "INBOX") -> dict:
        """{uid: ключ категории} для писем папки; без категорий — пусто."""
        return {}
