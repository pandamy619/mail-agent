# -*- coding: utf-8 -*-
"""
Gmail: категории вкладок («Промоакции», «Соцсети», «Оповещения»,
«Несортированные») по IMAP не видны как папки или флаги — только через
поиск расширением X-GM-RAW. Один запрос на категорию по диапазону UID;
письмо вне явных категорий считается «Несортированные» (category:primary
у Gmail включает и «Оповещения», поэтому напрямую не используется).
"""
import time

from .base import FALLBACK_NAMES, Provider

SEARCH_KEYS = ("promotions", "social", "updates", "forums")
CACHE_TTL = 600


class GmailProvider(Provider):
    name = "gmail"
    has_categories = True
    fallback_names = dict(FALLBACK_NAMES, **{
        "spam": ("[Gmail]/Spam", "[Gmail]/Спам") + FALLBACK_NAMES["spam"],
        "trash": ("[Gmail]/Trash", "[Gmail]/Корзина") + FALLBACK_NAMES["trash"],
        "sent": ("[Gmail]/Sent Mail", "[Gmail]/Отправленные") + FALLBACK_NAMES["sent"],
        "drafts": ("[Gmail]/Drafts", "[Gmail]/Черновики") + FALLBACK_NAMES["drafts"],
        "all": ("[Gmail]/All Mail", "[Gmail]/Вся почта"),
    })

    def __init__(self, account: dict):
        super().__init__(account)
        self._cache = {}      # uid → ключ категории
        self._cached_at = 0.0

    def categories(self, sess, uids: list, folder: str = "INBOX") -> dict:
        uids = sorted({int(u) for u in uids})
        if not uids:
            return {}
        if time.monotonic() - self._cached_at > CACHE_TTL:
            self._cache.clear()
        missing = [u for u in uids if u not in self._cache]
        if missing:
            self._lookup(sess, missing, folder)
        return {u: self._cache[u] for u in uids if u in self._cache}

    def _lookup(self, sess, uids: list, folder: str) -> None:
        lo, hi = uids[0], uids[-1]
        found = {}
        for key in SEARCH_KEYS:
            hits = sess.search_uids(f'UID {lo}:{hi} X-GM-RAW "category:{key}"',
                                    folder=folder)
            for u in hits:
                found.setdefault(int(u), key)
        for u in uids:
            self._cache[u] = found.get(u, "primary")
        if not self._cached_at:
            self._cached_at = time.monotonic()
