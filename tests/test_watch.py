# -*- coding: utf-8 -*-
"""Наблюдения за отправителями: признаки, совпадение, самообучение,
инструмент с гейтом, пуш в checker — без сети."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import proactive, toolbox, watches  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
from agent.tools import mail  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._file = watches.WATCH_FILE
        watches.WATCH_FILE = Path(self.tmp.name) / "watches.json"

    def tearDown(self):
        watches.WATCH_FILE = self._file
        self.tmp.cleanup()


class MatchTest(_Tmp):
    def test_parse(self):
        f = watches.parse_sender('"ФНС России" <No_Reply_LK@Service.Tax.Gov.ru>')
        self.assertEqual(f, {"name": "фнс россии", "address": "no_reply_lk@service.tax.gov.ru",
                             "domain": "service.tax.gov.ru"})

    def test_address_domain_name(self):
        w, created = watches.add("ФНС России <no_reply_lk@service.tax.gov.ru>", "Google")
        self.assertTrue(created)
        self.assertEqual(w["domain"], "service.tax.gov.ru")
        self.assertEqual(watches.match("x <no_reply_lk@service.tax.gov.ru>")[1], "address")
        self.assertEqual(watches.match("ФНС <no_reply_ucfns@service.tax.gov.ru>")[1], "domain")
        self.assertEqual(watches.match("Личный кабинет. ФНС России <other@nalog.ru>")[1], "name")
        self.assertIsNone(watches.match("Ozon <promo@ozon.ru>"))

    def test_generic_domain_not_used(self):
        w, _ = watches.add("Иван Петров <ivan@gmail.com>")
        self.assertEqual(w["domain"], "")
        self.assertIsNone(watches.match("Мария <maria@gmail.com>"))
        self.assertEqual(watches.match("Иван Петров <ivan.p@corp.ru>")[1], "name")

    def test_learning_and_hits(self):
        watches.add("ФНС России <no_reply_lk@service.tax.gov.ru>")
        w, how = watches.record_hit("ФНС <no_reply_ucfns@service.tax.gov.ru>")
        self.assertEqual(how, "domain")
        self.assertEqual(w["addresses"], ["no_reply_lk@service.tax.gov.ru",
                                          "no_reply_ucfns@service.tax.gov.ru"])
        self.assertEqual(watches.load()[0]["hits"], 1)
        self.assertEqual(watches.match("x <no_reply_ucfns@service.tax.gov.ru>")[1], "address")

    def test_duplicate_and_remove(self):
        watches.add("A <a@x.ru>")
        _, created = watches.add("A again <a@x.ru>")
        self.assertFalse(created)
        watches.add("B <b@y.ru>")
        self.assertEqual(len(watches.listing()), 2)
        removed = watches.remove(sender="B <b@y.ru>")
        self.assertEqual(removed["addresses"], ["b@y.ru"])
        watches.remove(n=1)
        self.assertEqual(watches.load(), [])
        with self.assertRaises(ValueError):
            watches.remove(n=1)


class ToolTest(_Tmp):
    def _conv(self, text):
        conv = Conversation(default_account="Google")
        conv.fmt_list([{"id": 42, "account": "Google", "sender": "ФНС России <lk@service.tax.gov.ru>",
                        "subject": "s", "age_str": ""}])
        conv.last_user_text = text
        return conv

    def test_gate(self):
        conv = self._conv("покажи письма")
        res = json.loads(toolbox.execute(conv, "watch", {"action": "add", "id": 42}))
        self.assertIn("отказано кодом", res["error"])
        self.assertEqual(watches.load(), [])

    def test_add_list_remove(self):
        conv = self._conv("следи за первым")
        res = json.loads(toolbox.execute(conv, "watch", {"action": "add", "id": 42}))
        self.assertIn("lk@service.tax.gov.ru", res["watching"])
        res = json.loads(toolbox.execute(conv, "watch", {"action": "list"}))
        self.assertEqual(len(res["watches"]), 1)
        conv.last_user_text = "перестань следить за этим"
        res = json.loads(toolbox.execute(conv, "watch", {"action": "remove", "id": 42}))
        self.assertIn("removed", res)
        self.assertEqual(watches.load(), [])


class NotifyTest(_Tmp):
    def test_push_and_important(self):
        watches.add("ФНС России <lk@service.tax.gov.ru>")
        self._prev = mail.preview
        mail.preview = lambda mid, acc, **k: "текст письма"
        sent = []
        rows = [{"id": 1, "account": "Google", "sender": "ФНС <ucfns@service.tax.gov.ru>",
                 "subject": "Требование", "received": 1.0},
                {"id": 2, "account": "Google", "sender": "Ozon <p@ozon.ru>", "subject": "Скидки"}]
        try:
            out = proactive.notify_watched(rows, lambda text, markup=None, parts=None: sent.append(text))
        finally:
            mail.preview = self._prev
        self.assertEqual([c["id"] for c in out], [1])
        self.assertIn("наблюдение: фнс россии (совпал домен)", out[0]["reason"])
        self.assertEqual(len(sent), 1)
        self.assertIn("👁 От ФНС", sent[0])
        self.assertIn("текст письма", sent[0])
        self.assertIn("новый адрес ucfns@service.tax.gov.ru", sent[0])


if __name__ == "__main__":
    unittest.main()
