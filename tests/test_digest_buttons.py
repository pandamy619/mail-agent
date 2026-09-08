# -*- coding: utf-8 -*-
"""Кнопки под дайджестом: клавиатура, показ категории по id, заявка
на перемещение промо — без сети."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import render  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
from agent.tools import mail, mail_actions  # noqa: E402
from interfaces import telegram_bot  # noqa: E402
from tests.test_digest import CARDS, IMPORTANT  # noqa: E402


class KeyboardTest(unittest.TestCase):
    def test_buttons_from_count_items(self):
        b = render.digest_blocks(datetime.now(), CARDS, IMPORTANT)
        self.assertEqual(sorted(b["count_items"]), ["Промоакции", "Соцсети"])
        kb = render.digest_keyboard(b["count_items"], "2026-09-08")
        texts = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
        self.assertEqual(texts, ["📂 Промоакции 1", "📂 Соцсети 1", "🗑 Промоакции за период в корзину"])
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "dg_show:promotions:2026-09-08")
        self.assertEqual(kb["inline_keyboard"][1][0]["callback_data"], "dg_trash:promotions:2026-09-08")

    def test_no_buttons_when_nothing(self):
        self.assertIsNone(render.digest_keyboard({}, "2026-09-08"))


class ShowIdsTest(unittest.TestCase):
    def setUp(self):
        self._orig = mail.fetch_by_ids
        mail.fetch_by_ids = lambda acc, ids: [
            {"id": i, "account": acc, "sender": f"s{i}", "subject": "t", "age_str": "",
             "category": "promotions"} for i in ids]

    def tearDown(self):
        mail.fetch_by_ids = self._orig

    def test_numbering_continues_after_digest_and_pages(self):
        conv = Conversation(default_account="Google")
        conv._numbered = {n: {"n": n, "id": 900 + n, "ids": [900 + n]} for n in range(1, 10)}
        items = [{"account": "Google", "id": 100 + i} for i in range(23)]
        page1 = conv.show_ids(items)
        self.assertEqual([c["n"] for c in page1], list(range(10, 20)))
        self.assertEqual(conv.number_hint("удали двенадцатое"), "письмо №12: id 102")
        self.assertEqual(conv.number_hint("покажи первое"), "письмо №1: id 901")  # дайджест на месте
        self.assertTrue(conv.more_available())
        page2 = conv.page_more(conv.last_list["token"])
        self.assertEqual([c["n"] for c in page2], list(range(20, 30)))
        page3 = conv.page_more(conv.last_list["token"])
        self.assertEqual(len(page3), 3)
        self.assertFalse(conv.more_available())
        self.assertEqual(conv.history[-1]["role"], "tool")


class _Bot(telegram_bot.Bot):
    def __init__(self):
        super().__init__("t", 1, accounts=[])
        self.sent = []

    def api(self, method, http_timeout=65, **params):
        return {}

    def send(self, chat_id, text, markup=None):
        self.sent.append((text, markup))

    def send_html(self, chat_id, parts, markup=None):
        self.sent.append(("\n".join(parts), markup))


class DigestButtonsTest(unittest.TestCase):
    def setUp(self):
        import agent.conversation as conv_mod
        self.tmp = tempfile.TemporaryDirectory()
        self._digest, self._done = conv_mod.DIGEST_FILE, telegram_bot.DIGEST_DONE_FILE
        conv_mod.DIGEST_FILE = Path(self.tmp.name) / "digest_cards.json"
        telegram_bot.DIGEST_DONE_FILE = Path(self.tmp.name) / "digest_done.json"
        conv_mod.DIGEST_FILE.write_text(json.dumps({
            "at": 1, "day": "2026-09-08",
            "rows": [{"n": 1, "id": 5, "ids": [5], "account": "Google"}],
            "groups": {"promotions": [{"account": "Google", "id": 11}, {"account": "Google", "id": 12}]}}))
        self._fetch, self._trash = mail.fetch_by_ids, mail_actions.trash_by_ids
        mail.fetch_by_ids = lambda acc, ids: [{"id": i, "account": acc, "sender": "p", "subject": "s",
                                               "age_str": "", "category": "promotions"} for i in ids]
        self.trashed = []
        mail_actions.trash_by_ids = lambda acc, ids: self.trashed.append((acc, ids)) or len(ids)
        self._previews = mail.add_previews
        mail.add_previews = lambda cards, **k: cards

    def tearDown(self):
        import agent.conversation as conv_mod
        conv_mod.DIGEST_FILE, telegram_bot.DIGEST_DONE_FILE = self._digest, self._done
        mail.fetch_by_ids, mail_actions.trash_by_ids = self._fetch, self._trash
        mail.add_previews = self._previews
        self.tmp.cleanup()

    @staticmethod
    def _press(data):
        return {"callback_query": {"id": "q", "from": {"id": 1},
                                   "message": {"chat": {"id": 1}, "message_id": 3}, "data": data}}

    def test_show_category(self):
        bot = _Bot()
        bot.handle(self._press("dg_show:promotions:2026-09-08"))
        text, markup = bot.sent[-1]
        self.assertIn("Промоакции за период: показано 2 из 2", text)
        self.assertIsNone(markup)
        self.assertEqual(bot.conv.number_hint("удали второе"), "письмо №2: id 11")

    def test_trash_two_steps_once_per_day(self):
        bot = _Bot()
        bot.handle(self._press("dg_trash:promotions:2026-09-08"))
        text, markup = bot.sent[-1]
        self.assertIn("В корзину 2 писем", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "dg_trash_yes:promotions:2026-09-08")
        self.assertEqual(self.trashed, [])
        bot.handle(self._press("dg_trash_yes:promotions:2026-09-08"))
        self.assertEqual(self.trashed, [("Google", [11, 12])])
        self.assertIn("перемещено 2 из 2", bot.sent[-1][0])
        bot.handle(self._press("dg_trash_yes:promotions:2026-09-08"))
        self.assertEqual(len(self.trashed), 1)
        self.assertIn("уже убирал", bot.sent[-1][0])

    def test_stale_day(self):
        bot = _Bot()
        bot.handle(self._press("dg_show:promotions:2026-09-01"))
        self.assertIn("устарел", bot.sent[-1][0])
        bot.handle(self._press("dg_trash_yes:promotions:2026-09-01"))
        self.assertIn("устарел", bot.sent[-1][0])
        self.assertEqual(self.trashed, [])


if __name__ == "__main__":
    unittest.main()
