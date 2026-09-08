# -*- coding: utf-8 -*-
"""Листание списков кодом («Ещё 10») — без сети."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import toolbox  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
from agent.tools import mail  # noqa: E402


class PageSliceTest(unittest.TestCase):
    def test_page(self):
        uids = list(range(1, 26))            # 25 писем, 25 — самое свежее
        self.assertEqual(mail._page(uids, 10, 0), list(range(16, 26)))
        self.assertEqual(mail._page(uids, 10, 10), list(range(6, 16)))
        self.assertEqual(mail._page(uids, 10, 20), [1, 2, 3, 4, 5])
        self.assertEqual(mail._page(uids, 10, 30), [])


class PagingTest(unittest.TestCase):
    def setUp(self):
        self._orig = mail.list_folder
        self.calls = []

        def fake(acc, folder, limit=10, offset=0):
            self.calls.append(offset)
            rows = [{"id": 100 - offset - i, "account": "Google", "sender": f"s{offset + i}",
                     "subject": "t", "folder": "[Gmail]/Спам", "folder_label": "Спам",
                     "age_str": ""} for i in range(min(limit, 25 - offset))]
            return rows, 25
        mail.list_folder = fake

    def tearDown(self):
        mail.list_folder = self._orig

    def test_more_pages_and_numbering(self):
        conv = Conversation(default_account="Google")
        conv._cards.clear()
        res = json.loads(toolbox.execute(conv, "mail_list", {"account": "Google", "folder": "spam"}))
        self.assertEqual((res["total"], res["shown"]), (25, 10))
        self.assertTrue(conv.more_available())
        token = conv.last_list["token"]
        page2 = conv.page_more(token)
        self.assertEqual([c["n"] for c in page2], list(range(11, 21)))
        self.assertEqual(self.calls, [0, 10])
        self.assertEqual(conv.history[-1]["role"], "tool")
        self.assertNotEqual(conv.last_list["token"], token)     # старая кнопка устарела
        self.assertEqual(conv.page_more(token), [])
        page3 = conv.page_more(conv.last_list["token"])
        self.assertEqual([c["n"] for c in page3], list(range(21, 26)))
        self.assertFalse(conv.more_available())
        self.assertEqual(conv.page_more(conv.last_list["token"]), [])

    def test_no_button_when_all_shown(self):
        mail.list_folder = lambda acc, folder, limit=10, offset=0: (
            [{"id": i, "account": "Google", "sender": "s", "subject": "t", "age_str": ""}
             for i in range(5)], 5)
        conv = Conversation(default_account="Google")
        toolbox.execute(conv, "mail_list", {"account": "Google", "folder": "spam"})
        self.assertFalse(conv.more_available())


class NumberHintTest(unittest.TestCase):
    def test_parse_numbers(self):
        from agent.conversation import list_numbers
        self.assertEqual(list_numbers("удали пятнадцатое"), [15])
        self.assertEqual(list_numbers("прочитай третье и №12"), [12, 3])
        self.assertEqual(list_numbers("покажи 3-е письмо"), [3])
        self.assertEqual(list_numbers("двадцать первое в корзину"), [21])
        self.assertEqual(list_numbers("что непрочитанного?"), [])

    def test_hint_from_last_list(self):
        conv = Conversation(default_account="Google")
        conv.fmt_list([{"id": 501, "account": "Google", "sender": "a", "subject": "s", "age_str": ""},
                       {"id": 502, "account": "Google", "sender": "b", "subject": "t", "age_str": "",
                        "folder": "[Gmail]/Спам", "folder_label": "Спам"}])
        conv._numbered = {c["n"]: c for c in conv._cards}
        self.assertEqual(conv.number_hint("удали второе"), "письмо №2: id 502, папка [Gmail]/Спам")
        self.assertEqual(conv.number_hint("прочитай первое"), "письмо №1: id 501")
        self.assertEqual(conv.number_hint("удали девятое"), "")


if __name__ == "__main__":
    unittest.main()
