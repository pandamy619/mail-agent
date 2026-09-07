# -*- coding: utf-8 -*-
"""Рендер списков писем кодом: группировка, экранирование, упаковка."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import render  # noqa: E402
from agent.conversation import Conversation  # noqa: E402

NOW = time.time()
CARDS = [
    {"n": 1, "id": 10, "account": "Google", "sender": "Ozon <promo@ozon.ru>",
     "subject": "Скидки <b>70%</b>", "received": NOW - 60, "unread": True,
     "category": "Промоакции", "preview": "Только сегодня & только для вас"},
    {"n": 2, "id": 11, "account": "Google", "sender": "GitLab", "subject": "Pipeline failed",
     "received": NOW - 3 * 86400, "unread": False, "category": "Оповещения"},
    {"n": 3, "id": 12, "account": "Google", "sender": "Ozon", "subject": "Ещё скидки",
     "received": NOW - 120, "unread": False, "category": "Промоакции"},
    {"n": 4, "id": 5, "account": "Yandex", "sender": "Иван", "subject": "Без категории",
     "received": None, "unread": True, "category": "", "age_str": "5 мин назад"},
]


class HtmlTest(unittest.TestCase):
    def test_groups_in_order_of_first_appearance(self):
        parts = render.cards_html(CARDS)
        heads = [p for p in parts if p.startswith("📂")]
        self.assertEqual(heads, ["📂 <b>Промоакции</b> (2)", "📂 <b>Оповещения</b> (1)"])
        self.assertEqual(len(parts), 2 + 4)
        self.assertTrue(parts[-1].startswith("4. <b>Иван</b>"))   # без заголовка

    def test_escaping_and_preview(self):
        p = render.card_html(CARDS[0])
        self.assertIn("&lt;b&gt;70%&lt;/b&gt;", p)
        self.assertIn("&lt;promo@ozon.ru&gt;", p)
        self.assertIn("<blockquote expandable>Только сегодня &amp; только для вас</blockquote>", p)
        self.assertIn("· ●", p)
        self.assertNotIn("●", render.card_html(CARDS[1]))

    def test_dates(self):
        self.assertRegex(render._when(CARDS[0]), r"^\d\d:\d\d$")
        self.assertRegex(render._when({"received": NOW - 400 * 86400}), r"^\d\d\.\d\d\.\d\d$")
        self.assertEqual(render._when(CARDS[3]), "5 мин назад")

    def test_pack_respects_limit(self):
        parts = ["x" * 100] * 10
        chunks = render.pack(parts, limit=350)
        self.assertEqual([len(c.split("\n\n")) for c in chunks], [3, 3, 3, 1])
        self.assertTrue(all(len(c) <= 350 for c in chunks))
        self.assertEqual(render.pack(["a" * 500], limit=100), ["a" * 100])


class TextTest(unittest.TestCase):
    def test_text_list(self):
        out = render.cards_text(CARDS)
        self.assertIn("📂 Промоакции (2)", out)
        self.assertIn("●  1. Ozon <promo@ozon.ru>", out)
        self.assertIn("› Только сегодня", out)
        self.assertIn(" 4. Иван", out)


class TurnCardsTest(unittest.TestCase):
    def test_numbering_across_tools_and_dedupe(self):
        conv = Conversation()
        rows = [{"id": 1, "account": "Google", "sender": "a", "subject": "s", "unread": True,
                 "received": NOW, "age_str": "1 мин назад", "category": "social"},
                {"id": 2, "account": "Google", "sender": "b", "subject": "t", "unread": False,
                 "received": NOW, "age_str": "2 мин назад"}]
        conv.fmt_list(rows)
        conv.fmt_list([rows[1], {"id": 3, "account": "Google", "sender": "c", "subject": "u",
                                 "received": NOW, "age_str": ""}])
        cards = conv.turn_cards()
        self.assertEqual([c["n"] for c in cards], [1, 2, 3])
        self.assertEqual(cards[0]["category"], "Соцсети")
        self.assertEqual(cards[1]["category"], "")
        self.assertEqual(conv.turn_categories(), ["Соцсети"])


if __name__ == "__main__":
    unittest.main()
