# -*- coding: utf-8 -*-
"""Дайджест: агрегация по отправителям, рендер, накопление карточек в состоянии."""
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import render  # noqa: E402
from agent import proactive as check_mail  # noqa: E402

NOW = time.time()


def _c(i, sender, subject, cat, age=60, **extra):
    d = {"n": 0, "id": i, "account": "Google", "sender": sender, "subject": subject,
         "received": NOW - age, "unread": False, "category": cat}
    d.update(extra)
    return d


CARDS = [
    _c(1, "GitLab <noreply@gitlab.com>", "Pipeline #128 failed", "Оповещения", 60),
    _c(2, "GitLab <noreply@gitlab.com>", "Pipeline #127 failed", "Оповещения", 600),
    _c(3, "Bybit <noreply@bybit.com>", "Вход", "Оповещения", 300),
    _c(4, "Ozon <promo@ozon.ru>", "Скидки", "Промоакции", 100),
    _c(5, "LinkedIn <m@linkedin.com>", "Уведомления", "Соцсети", 100),
    _c(6, "Иван Петров <ivan@corp.ru>", "Срочно: договор", "Несортированные", 200, unread=True),
    _c(7, "Мария <maria@gmail.com>", "Оплата", "Несортированные", 400),
]
IMPORTANT = [dict(CARDS[5], reason="срочность", preview="Нужна подпись…")]


class AggregateTest(unittest.TestCase):
    def test_by_sender_latest_first(self):
        rows = render.aggregate_senders(CARDS[:3])
        self.assertEqual([(r["sender"], r["count"]) for r in rows],
                         [("GitLab", 2), ("Bybit", 1)])
        self.assertEqual(rows[0]["latest"]["id"], 1)


class BlocksTest(unittest.TestCase):
    def test_structure(self):
        b = render.digest_blocks(datetime.now(), CARDS, IMPORTANT)
        self.assertEqual(b["total"], 7)
        self.assertEqual([c["n"] for c in b["important"]], [1])
        labels = [g["label"] for g in b["groups"]]
        self.assertEqual(labels, ["Оповещения", "Несортированные"])
        self.assertEqual(dict(b["counts"]), {"Промоакции": 1, "Соцсети": 1})
        primary = b["groups"][1]
        self.assertEqual(primary["total"], 1)            # важное исключено
        self.assertEqual(primary["rows"][0]["sender"], "Мария")
        self.assertEqual(b["groups"][0]["rows"][0]["n"], 2)   # сквозная нумерация

    def test_senders_cap(self):
        many = [_c(100 + i, f"s{i} <s{i}@x.ru>", "t", "Оповещения", i) for i in range(12)]
        g = render.digest_blocks(datetime.now(), many, [])["groups"][0]
        self.assertEqual(len(g["rows"]), render.SENDERS_CAP)
        self.assertEqual(g["more"], 12 - render.SENDERS_CAP)


class PluralTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(render._more_senders(1), "…и ещё 1 отправитель")
        self.assertEqual(render._more_senders(3), "…и ещё 3 отправителя")
        self.assertEqual(render._more_senders(11), "…и ещё 11 отправителей")
        self.assertEqual(render._more_senders(22), "…и ещё 22 отправителя")


class RenderTest(unittest.TestCase):
    def test_html(self):
        parts = render.digest_html(datetime(2026, 9, 7, 8, 0), CARDS, IMPORTANT)
        self.assertTrue(parts[0].startswith("☀️ <b>Дайджест 07.09, 08:00</b> · новых 7, важных 1"))
        self.assertEqual(parts[1], "🔔 <b>Важное</b> (1)")
        self.assertIn("<blockquote>срочность</blockquote>", parts[2])
        self.assertIn("<blockquote expandable>Нужна подпись…</blockquote>", parts[2])
        self.assertIn("· ●", parts[2])
        self.assertIn("<b>GitLab</b> ×2", parts[3])
        self.assertIn("📂 <b>Промоакции</b>: 1 · 📂 <b>Соцсети</b>: 1", parts[-2])

    def test_empty(self):
        parts = render.digest_html(datetime.now(), [], [])
        self.assertEqual(len(parts), 1)
        self.assertIn("тихо", parts[0])
        self.assertIn("тихо", render.digest_text(datetime.now(), [], []))

    def test_text(self):
        t = render.digest_text(datetime.now(), CARDS, IMPORTANT)
        self.assertIn("🔔 Важное (1)", t)
        self.assertIn("— срочность", t)
        self.assertIn("GitLab ×2", t)


class NumberedTest(unittest.TestCase):
    def test_numbered_rows_and_group_ids(self):
        b = render.digest_blocks(datetime.now(), CARDS, IMPORTANT)
        rows = render.digest_numbered(b)
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(rows[0]["ids"], [6])
        gitlab = next(r for r in rows if r["sender"] == "GitLab")
        self.assertEqual((gitlab["count"], gitlab["ids"]), (2, [2, 1]))
        self.assertEqual(rows[-1]["sender"], "Мария")
        parts = render.digest_html(datetime.now(), CARDS, IMPORTANT)
        self.assertIn("Номера действуют в чате", parts[-1])


class AdoptDigestTest(unittest.TestCase):
    def test_adopt_and_hints(self):
        import json, tempfile, time
        from agent.conversation import Conversation
        b = render.digest_blocks(datetime.now(), CARDS, IMPORTANT)
        rows = render.digest_numbered(b)
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "digest_cards.json"
        path.write_text(json.dumps({"at": time.time(), "rows": rows}), encoding="utf-8")
        conv = Conversation(default_account="Google")
        self.assertTrue(conv.adopt_digest(path))
        self.assertEqual(conv.number_hint("покажи первое"), "письмо №1: id 6")
        n_gitlab = next(r["n"] for r in rows if r["sender"] == "GitLab")
        self.assertEqual(conv.number_hint(f"удали {n_gitlab}-е"),
                         f"письма №{n_gitlab} (GitLab ×2): ids 2, 1")
        # свежий список в диалоге важнее старого дайджеста
        conv.fmt_list([{"id": 900, "account": "Google", "sender": "z", "subject": "s", "age_str": ""}])
        conv._numbered = {c["n"]: c for c in conv._cards}; conv._numbered_at = time.time() + 1
        self.assertFalse(conv.adopt_digest(path))
        self.assertEqual(conv.number_hint("удали первое"), "письмо №1: id 900")
        tmp.cleanup()

    def test_save_rows(self):
        import json, tempfile
        tmp = tempfile.TemporaryDirectory()
        orig_dir, orig_file = check_mail.STATE_DIR, check_mail.DIGEST_FILE
        check_mail.STATE_DIR = Path(tmp.name); check_mail.DIGEST_FILE = Path(tmp.name) / "d.json"
        try:
            check_mail.save_digest_rows([{"n": 1, "id": 5, "ids": [5]}], datetime(2026, 9, 8, 8, 0))
            data = json.loads(check_mail.DIGEST_FILE.read_text())
            self.assertEqual(data["rows"][0]["id"], 5)
            self.assertGreater(data["at"], 0)
        finally:
            check_mail.STATE_DIR, check_mail.DIGEST_FILE = orig_dir, orig_file
            tmp.cleanup()


class AccumulateTest(unittest.TestCase):
    def test_cards_and_counts(self):
        stt = check_mail._fresh_stats(datetime.now())
        rows = [{"account": "Google", "id": i, "sender": "a", "subject": "b",
                 "received": NOW - i, "category": "updates", "unread": True,
                 "age_str": "x", "extra": "не хранится"} for i in range(3)]
        check_mail.accumulate(stt, rows, [dict(rows[0], reason="важно")])
        self.assertEqual(stt["new"], 3)
        self.assertEqual(stt["per_account"], {"Google": 3})
        self.assertEqual([c["id"] for c in stt["cards"]], [0, 1, 2])
        self.assertNotIn("extra", stt["cards"][0])
        self.assertEqual(stt["important"][0]["reason"], "важно")
        self.assertEqual(stt["important"][0]["category"], "updates")

    def test_cap_keeps_newest(self):
        stt = check_mail._fresh_stats(datetime.now())
        check_mail.accumulate(stt, [{"account": "G", "id": 1, "sender": "", "subject": ""}], [])
        check_mail.accumulate(stt, [{"account": "G", "id": i, "sender": "", "subject": ""}
                                    for i in range(2, 2 + check_mail.CARDS_CAP)], [])
        self.assertEqual(len(stt["cards"]), check_mail.CARDS_CAP)
        self.assertEqual(stt["cards"][0]["id"], 2)
        self.assertNotIn(1, [c["id"] for c in stt["cards"]])


if __name__ == "__main__":
    unittest.main()
