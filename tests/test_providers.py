# -*- coding: utf-8 -*-
"""Провайдеры ящиков и категории Gmail — без сети."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import mail_index, providers  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
from agent.providers.gmail import GmailProvider  # noqa: E402


class FactoryTest(unittest.TestCase):
    def setUp(self):
        providers._instances.clear()

    def test_gmail_by_host(self):
        p = providers.for_account({"name": "G", "host": "imap.gmail.com"})
        self.assertIsInstance(p, GmailProvider)
        self.assertTrue(p.has_categories)

    def test_other_hosts_are_plain_imap(self):
        for host in ("imap.yandex.ru", "imap.mail.me.com", "mail.example.org"):
            p = providers.for_account({"name": host, "host": host})
            self.assertIs(type(p), providers.Provider)
            self.assertFalse(p.has_categories)
            self.assertEqual(p.categories(None, [1, 2]), {})

    def test_cached_per_account(self):
        a = providers.for_account({"name": "G", "host": "imap.gmail.com"})
        b = providers.for_account({"name": "G", "host": "imap.gmail.com"})
        self.assertIs(a, b)

    def test_labels(self):
        self.assertEqual(providers.label("promotions"), "Промоакции")
        self.assertEqual(providers.label(""), "")


class _FakeSession:
    """search_uids отвечает по подстроке category:<key> в критерии."""

    def __init__(self, by_key: dict):
        self.by_key = by_key
        self.calls = []

    def search_uids(self, criteria, folder="INBOX"):
        self.calls.append(criteria)
        for key, ids in self.by_key.items():
            if f"category:{key}" in criteria:
                return sorted(ids)
        return []


class GmailCategoriesTest(unittest.TestCase):
    def test_categories_and_primary_fallback(self):
        sess = _FakeSession({"promotions": [1, 5], "social": [2], "updates": [3, 5]})
        p = GmailProvider({"name": "G", "host": "imap.gmail.com"})
        cats = p.categories(sess, [1, 2, 3, 4, 5])
        self.assertEqual(cats, {1: "promotions", 2: "social", 3: "updates",
                                4: "primary", 5: "promotions"})
        self.assertEqual(len(sess.calls), 4)
        self.assertIn("UID 1:5 X-GM-RAW", sess.calls[0])

    def test_cache_avoids_repeat_searches(self):
        sess = _FakeSession({"promotions": [1]})
        p = GmailProvider({"name": "G", "host": "imap.gmail.com"})
        p.categories(sess, [1, 2])
        p.categories(sess, [1, 2])
        self.assertEqual(len(sess.calls), 4)
        p.categories(sess, [3])
        self.assertEqual(len(sess.calls), 8)


class IndexCategoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db = mail_index.DB_PATH
        mail_index.DB_PATH = Path(self.tmp.name) / "index.db"
        rows = [{"id": 1, "sender": "Ozon <promo@ozon.ru>", "subject": "Скидки",
                 "age_sec": 10, "unread": True, "category": "promotions"},
                {"id": 2, "sender": "Иван <i@x.ru>", "subject": "Договор",
                 "age_sec": 20, "unread": False, "category": "primary"},
                {"id": 3, "sender": "Yandex <a@ya.ru>", "subject": "Без категории",
                 "age_sec": 30, "unread": False}]
        mail_index.upsert("Google", rows)

    def tearDown(self):
        mail_index.DB_PATH = self._db
        self.tmp.cleanup()

    def test_filter_by_category(self):
        res = mail_index.search(account="Google", category="promotions")
        self.assertEqual([r["id"] for r in res["rows"]], [1])
        self.assertEqual(res["rows"][0]["category"], "promotions")
        self.assertEqual(mail_index.search(account="Google")["total"], 3)

    def test_get_by_ids_has_category(self):
        info = mail_index.get_by_ids("Google", [2, 3])
        self.assertEqual(info[2]["category"], "primary")
        self.assertEqual(info[3]["category"], "")

    def test_search_ids_by_category(self):
        self.assertEqual(mail_index.search_ids("Google", category="promotions"), [1])


class CardCategoryTest(unittest.TestCase):
    def test_fmt_list_adds_label_and_tracks_turn(self):
        conv = Conversation()
        out = conv.fmt_list([{"id": 1, "age_str": "1 мин назад", "unread": True,
                               "sender": "a", "subject": "b", "category": "social"},
                              {"id": 2, "age_str": "2 мин назад", "unread": False,
                               "sender": "c", "subject": "d"}])
        self.assertIn('"category": "Соцсети"', out)
        self.assertEqual(conv.turn_categories(), ["Соцсети"])


if __name__ == "__main__":
    unittest.main()
