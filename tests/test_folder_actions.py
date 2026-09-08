# -*- coding: utf-8 -*-
"""Перемещение из папки и поиск по папке — на поддельной сессии, без сети."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import toolbox  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
from agent.tools import mail, mail_actions  # noqa: E402


class _Sess:
    """Ящик с папками {имя: [uid…]}; MOVE переносит uid между ними."""

    def __init__(self, folders):
        self.f = {k: list(v) for k, v in folders.items()}
        self.selected = None
        self.log = []

    def existing(self, ids, folder="INBOX"):
        return {i for i in ids if i in self.f.get(folder, [])}

    def select(self, folder="INBOX", readonly=True):
        self.selected = folder
        return len(self.f.get(folder, []))

    def has_cap(self, cap):
        return True

    def uid(self, command, *args, label=None):
        self.log.append((command, self.selected, args))
        if command == "MOVE":
            ids = [int(x) for x in args[0].replace(":", ",").split(",")]
            target = args[1].strip('"')
            for i in ids:
                if i in self.f[self.selected]:
                    self.f[self.selected].remove(i)
                    self.f.setdefault(target, []).append(i)
        return []


class MoveFromFolderTest(unittest.TestCase):
    def test_move_from_spam_to_inbox(self):
        s = _Sess({"INBOX": [1, 2], "[Gmail]/Спам": [7, 8, 9]})
        done = mail_actions._move_uids(s, [7, 9, 99], "INBOX", "t", folder="[Gmail]/Спам")
        self.assertEqual(done, [7, 9])
        self.assertEqual(s.f["INBOX"], [1, 2, 7, 9])
        self.assertEqual(s.f["[Gmail]/Спам"], [8])
        self.assertEqual(s.log[0][1], "[Gmail]/Спам")      # MOVE выполнен из спама

    def test_same_folder_rejected(self):
        s = _Sess({"INBOX": [1]})
        with self.assertRaises(mail_actions.MailError):
            mail_actions._move_uids(s, [1], "INBOX", "t", folder="INBOX")


class SearchFolderTest(unittest.TestCase):
    def setUp(self):
        self._iter, self._resolve, self._acc = mail.iter_chunks, mail.resolve_folder, mail.resolve_account
        rows = [{"id": 30 - i, "account": "Google", "sender": f"{'Litres' if i % 3 else 'GitLab'} <a{i}@x.ru>",
                 "subject": f"тема {i}", "folder": "[Gmail]/Спам", "folder_label": "Спам", "age_str": ""}
                for i in range(30)]
        mail.iter_chunks = lambda acc, chunk=500, newest_first=True, folder="INBOX", folder_label="", cap=0: \
            iter([(30, 0, rows[:20]), (30, 20, rows[20:])])
        mail.resolve_folder = lambda acc, name: ("[Gmail]/Спам", "Спам")
        mail.resolve_account = lambda name: "Google"

    def tearDown(self):
        mail.iter_chunks, mail.resolve_folder, mail.resolve_account = self._iter, self._resolve, self._acc

    def test_filter_and_paging(self):
        page, total = mail.search_folder("Google", "spam", sender_contains="git-lab", limit=5)
        self.assertEqual(total, 10)
        self.assertEqual(len(page), 5)
        self.assertTrue(all("GitLab" in r["sender"] for r in page))
        page2, _ = mail.search_folder("Google", "spam", sender_contains="gitlab", limit=5, offset=5)
        self.assertEqual([r["id"] for r in page2][0], page[-1]["id"] - 3)

    def test_tool_uses_folder_search(self):
        conv = Conversation(default_account="Google")
        res = json.loads(toolbox.execute(conv, "mail_list", {"account": "Google", "folder": "spam",
                                                             "sender_contains": "litres"}))
        self.assertEqual(res["total"], 20)
        self.assertIn("живой поиск по папке", res["note"])
        self.assertEqual(res["messages"][0]["folder"], "[Gmail]/Спам")


class PendingFromFolderTest(unittest.TestCase):
    def setUp(self):
        self._resolve, self._folder = mail.resolve_account, mail.resolve_folder
        mail.resolve_account = lambda name: "Google"
        mail.resolve_folder = lambda acc, name: ("[Gmail]/Спам", "Спам") if name == "spam" else ("INBOX", "Входящие")
        self._mb = mail_actions.resolve_mailbox
        mail_actions.resolve_mailbox = lambda acc, name: "INBOX" if name.lower() in ("inbox", "входящие") else name

    def tearDown(self):
        mail.resolve_account, mail.resolve_folder = self._resolve, self._folder
        mail_actions.resolve_mailbox = self._mb

    def test_not_spam_pending(self):
        conv = Conversation(default_account="Google")
        res = json.loads(toolbox.execute(conv, "mail_move", {"account": "Google", "folder": "spam",
                                                             "ids": [7, 9], "target": "inbox"}))
        self.assertTrue(res.get("pending"))
        self.assertEqual(res["summary"], "2 писем из Google (Спам) → папка «INBOX»")
        self.assertEqual(conv.pending["folder"], "[Gmail]/Спам")
        self.assertEqual(conv.pending["op"], "move")

    def test_filter_in_folder_pending(self):
        conv = Conversation(default_account="Google")
        res = json.loads(toolbox.execute(conv, "mail_move", {"account": "Google", "folder": "spam",
                                                             "sender_contains": "litres", "target": "trash"}))
        self.assertTrue(res.get("pending"))
        self.assertIn("(Спам) → корзина", res["summary"])
        self.assertEqual(conv.pending["op"], "trash_filter")
        self.assertEqual(conv.pending["folder"], "[Gmail]/Спам")


if __name__ == "__main__":
    unittest.main()
