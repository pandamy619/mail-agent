# -*- coding: utf-8 -*-
"""Тесты без сети: бюджет истории по токенам и UID-курсор новых писем."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from agent import conversation  # noqa: E402
from agent.conversation import Conversation  # noqa: E402
import check_mail  # noqa: E402


def _conv(n_turns: int, tool_chars: int) -> Conversation:
    """Диалог: система + n ходов «пользователь → вызов → результат → ответ»."""
    conv = Conversation(default_account="Google", accounts=[])
    conv.history_budget = lambda: 3000   # токенов ≈ 9000 символов
    h = conv.history
    for i in range(n_turns):
        h.append({"role": "user", "content": f"вопрос {i}"})
        h.append({"role": "assistant", "content": "",
                  "tool_calls": [{"function": {"name": "list_recent",
                                               "arguments": {"account": "Google"}}}]})
        h.append({"role": "tool", "tool_name": "list_recent", "content": "x" * tool_chars})
        h.append({"role": "assistant", "content": f"ответ {i}"})
    return conv


class HistoryBudgetTest(unittest.TestCase):
    def test_short_history_untouched(self):
        conv = _conv(2, 100)
        self.assertEqual(conv.trimmed(), conv.history)

    def test_big_tool_results_trimmed_to_budget(self):
        conv = _conv(6, 4000)           # 6 ходов по ~1400 токенов
        h, t = conv.history, conv.trimmed()
        self.assertIs(t[0], h[0])       # системный промпт на месте
        self.assertEqual(t[1]["role"], "user")   # хвост начинается с пользователя
        self.assertEqual(t[-1], h[-1])
        self.assertLessEqual(sum(conversation._est_tokens(m) for m in t[1:]), 3000)

    def test_current_turn_never_cut(self):
        conv = _conv(1, 100)
        h = conv.history
        h.append({"role": "user", "content": "текущий вопрос"})
        h.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_mail", "arguments": {}}}]})
        h.append({"role": "tool", "tool_name": "read_mail", "content": "y" * 20000})  # больше бюджета
        t = conv.trimmed()
        self.assertEqual(t[1]["content"], "текущий вопрос")
        self.assertEqual(len(t), 4)

    def test_message_cap_still_applies(self):
        conv = _conv(20, 10)            # 80 сообщений
        conv.history_budget = lambda: 10 ** 9
        t = conv.trimmed()
        self.assertLessEqual(len(t), conversation.MAX_HISTORY + 1)
        self.assertEqual(t[1]["role"], "user")


class SingleAccountDefaultTest(unittest.TestCase):
    def test_single_account_becomes_default(self):
        conv = Conversation(default_account=None, accounts=[{"name": "Google", "email": "x"}])
        self.assertIn("Ящик по умолчанию: Google", conv.history[0]["content"])

    def test_several_accounts_still_ask(self):
        conv = Conversation(default_account=None, accounts=[{"name": "Google", "email": "x"},
                                                            {"name": "Yandex", "email": "y"}])
        self.assertIn("СНАЧАЛА спроси", conv.history[0]["content"])

    def test_explicit_default_wins(self):
        conv = Conversation(default_account="Yandex", accounts=[{"name": "Google", "email": "x"}])
        self.assertIn("Ящик по умолчанию: Yandex", conv.history[0]["content"])


class CursorTest(unittest.TestCase):
    def setUp(self):
        self._accs = check_mail.mail.accounts_info
        self._new = check_mail.mail.new_since
        check_mail.mail.accounts_info = lambda refresh=False: [{"name": "Google", "email": "x"}]
        self.calls = []

    def tearDown(self):
        check_mail.mail.accounts_info = self._accs
        check_mail.mail.new_since = self._new

    def _fake(self, rows, newest, reset=False):
        def new_since(acc, last, cap=500):
            self.calls.append((acc, last, cap))
            return rows, newest, reset
        check_mail.mail.new_since = new_since

    def test_first_run_baseline(self):
        self._fake([], 500)
        st = {"cursor": {}}
        self.assertEqual(check_mail.collect_new(st), [])
        self.assertEqual(st["cursor"], {"Google": 500})
        self.assertEqual(self.calls[0][1], None)

    def test_new_mail_advances_cursor(self):
        rows = [{"id": 502, "account": "Google"}, {"id": 501, "account": "Google"}]
        self._fake(rows, 502)
        st = {"cursor": {"Google": 500}}
        self.assertEqual(check_mail.collect_new(st), rows)
        self.assertEqual(st["cursor"]["Google"], 502)
        self.assertEqual(self.calls[0][1], 500)

    def test_uidvalidity_reset_makes_new_baseline(self):
        self._fake([], 7, reset=True)
        st = {"cursor": {"Google": 500}}
        self.assertEqual(check_mail.collect_new(st), [])
        self.assertEqual(st["cursor"]["Google"], 7)

    def test_legacy_seen_migrates(self):
        self._fake([], 37282)
        st = {"seen": {"Google": ["37282", "37281", "37276"]}}
        check_mail.collect_new(st)
        self.assertNotIn("seen", st)
        self.assertEqual(self.calls[0][1], 37282)
        self.assertEqual(st["cursor"]["Google"], 37282)


if __name__ == "__main__":
    unittest.main()
