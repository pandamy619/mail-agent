# -*- coding: utf-8 -*-
"""Роли папок в провайдерах и заявка на очистку папки — без сети."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import core, providers  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
from agent.providers.gmail import GmailProvider  # noqa: E402
from agent.tools import mail_actions  # noqa: E402


class _Sess:
    def __init__(self, folders):
        self._f = [{"name": n, "flags": set(f), "delim": "/"} for n, f in folders]

    def folders(self, refresh=False):
        return self._f


GMAIL = [("INBOX", ()), ("[Gmail]/Спам", ("\\junk",)), ("[Gmail]/Корзина", ("\\trash",)),
         ("[Gmail]/Отправленные", ("\\sent",)), ("[Gmail]/Вся почта", ("\\all",)), ("Prog", ())]
NOFLAGS = [("INBOX", ()), ("Spam", ()), ("Deleted Messages", ()), ("Sent Messages", ())]


class RoleTest(unittest.TestCase):
    def test_role_words(self):
        for w, r in (("спам", "spam"), ("Spam", "spam"), ("корзина", "trash"),
                     ("удалённые", "trash"), ("Вся почта", "all"), ("prog", None)):
            self.assertEqual(providers.role_of(w), r)
        self.assertEqual(providers.role_label("spam"), "Спам")

    def test_by_server_flags(self):
        p = GmailProvider({"name": "G", "host": "imap.gmail.com"})
        s = _Sess(GMAIL)
        self.assertEqual(p.folder(s, "spam"), "[Gmail]/Спам")
        self.assertEqual(p.folder(s, "trash"), "[Gmail]/Корзина")
        self.assertEqual(p.folder(s, "all"), "[Gmail]/Вся почта")
        self.assertEqual(p.folder(s, "inbox"), "INBOX")
        self.assertEqual(p.folder(s, "archive"), "")

    def test_fallback_names_without_flags(self):
        p = providers.Provider({"name": "I", "host": "imap.mail.me.com"})
        s = _Sess(NOFLAGS)
        self.assertEqual(p.folder(s, "spam"), "Spam")
        self.assertEqual(p.folder(s, "trash"), "Deleted Messages")
        self.assertEqual(p.folder(s, "sent"), "Sent Messages")
        self.assertEqual(p.folder(s, "drafts"), "")

    def test_config_override_wins(self):
        p = providers.Provider({"name": "Y", "host": "imap.yandex.ru", "trash": "Удалённые"})
        self.assertEqual(p.folder(_Sess(GMAIL), "trash"), "Удалённые")


class EmptyFolderTest(unittest.TestCase):
    def setUp(self):
        self.conv = Conversation()
        self._count = mail_actions.count_folder
        self._resolve = core.mail.resolve_account
        mail_actions.count_folder = lambda acc, role: {"trash": 3, "spam": 50}[role]
        core.mail.resolve_account = lambda name: "Google"

    def tearDown(self):
        mail_actions.count_folder = self._count
        core.mail.resolve_account = self._resolve

    def test_spam_pending(self):
        res = json.loads(core.execute_tool(self.conv, "empty_folder", {"account": "Google", "folder": "спам"}))
        self.assertTrue(res.get("pending"))
        self.assertIn("«Спам»", res["summary"])
        self.assertEqual(res["count"], 50)
        self.assertEqual(self.conv.pending["target"], "spam")

    def test_only_trash_and_spam(self):
        res = json.loads(core.execute_tool(self.conv, "empty_folder", {"account": "Google", "folder": "sent"}))
        self.assertIn("error", res)
        self.assertFalse(self.conv.has_pending())
        with self.assertRaises(mail_actions.MailError):
            mail_actions.empty_folder("Google", "sent")


class FolderCardTest(unittest.TestCase):
    def test_folder_card_groups_by_folder_label(self):
        conv = Conversation()
        out = json.loads(conv.fmt_list([{"id": 7, "account": "Google", "sender": "x",
                                         "subject": "y", "folder": "[Gmail]/Спам",
                                         "folder_label": "Спам", "age_str": ""}]))
        self.assertEqual(out["messages"][0]["folder"], "[Gmail]/Спам")
        self.assertEqual(conv.turn_cards()[0]["category"], "Спам")


if __name__ == "__main__":
    unittest.main()
