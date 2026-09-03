# -*- coding: utf-8 -*-
"""Тесты парсера config.yaml и чтения секретов из .env."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config  # noqa: E402

SAMPLE = """\
# комментарий
llm:
  base_url: "http://localhost:11434/v1"  # URL с двоеточиями и комментарием
  model: "qwen3:14b"
  timeout: 600
  ratio: 0.5

mail:
  default_account: ""
  batch_limit: 25   # число с комментарием
  accounts:
    Google:
      host: imap.gmail.com
      env: IMAP_GOOGLE
    Yandex:
      host: imap.yandex.ru
      port: 993
      env: IMAP_YANDEX
      trash: "Корзина"

proactive:
  enabled: true
  quiet_hours: "23:00-07:00"
  digest_enabled: no
"""


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.yaml"
        self.path.write_text(SAMPLE, encoding="utf-8")
        self.cfg = config.load(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flat_values(self):
        self.assertEqual(self.cfg["llm"]["base_url"], "http://localhost:11434/v1")
        self.assertEqual(self.cfg["llm"]["model"], "qwen3:14b")
        self.assertEqual(self.cfg["llm"]["timeout"], 600)
        self.assertEqual(self.cfg["llm"]["ratio"], 0.5)

    def test_empty_string_and_comment(self):
        self.assertEqual(self.cfg["mail"]["default_account"], "")
        self.assertEqual(self.cfg["mail"]["batch_limit"], 25)

    def test_booleans(self):
        self.assertIs(self.cfg["proactive"]["enabled"], True)
        self.assertIs(self.cfg["proactive"]["digest_enabled"], False)
        self.assertEqual(self.cfg["proactive"]["quiet_hours"], "23:00-07:00")

    def test_nested_accounts(self):
        accs = self.cfg["mail"]["accounts"]
        self.assertEqual(list(accs), ["Google", "Yandex"])
        self.assertEqual(accs["Google"], {"host": "imap.gmail.com", "env": "IMAP_GOOGLE"})
        self.assertEqual(accs["Yandex"]["trash"], "Корзина")
        self.assertEqual(accs["Yandex"]["port"], 993)

    def test_section_after_nested_block(self):
        # после глубокой вложенности следующая секция верхнего уровня не теряется
        self.assertIn("proactive", self.cfg)
        self.assertNotIn("proactive", self.cfg["mail"])


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.yaml"
        self.cfg_path.write_text(SAMPLE, encoding="utf-8")
        self.env_path = Path(self.tmp.name) / ".env"
        self.env_path.write_text(
            "IMAP_GOOGLE_USER=me@gmail.com\nIMAP_GOOGLE_PASS='abcd efgh'\n"
            "# IMAP_YANDEX_USER=\n", encoding="utf-8")
        for k in ("IMAP_GOOGLE_USER", "IMAP_GOOGLE_PASS", "IMAP_YANDEX_USER",
                  "IMAP_YANDEX_PASS"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("IMAP_GOOGLE_PASS", None)

    def test_accounts_list(self):
        accs = config.accounts(config.load(self.cfg_path))
        self.assertEqual([a["name"] for a in accs], ["Google", "Yandex"])
        self.assertEqual(accs[0]["port"], 993)
        self.assertIsNone(accs[0]["trash"])
        self.assertEqual(accs[1]["trash"], "Корзина")
        self.assertNotIn("password", accs[0])

    def test_credentials_from_env_file(self):
        acc = config.accounts(config.load(self.cfg_path))[0]
        self.assertEqual(config.credentials(acc, env_path=self.env_path),
                         ("me@gmail.com", "abcd efgh"))

    def test_environment_overrides_file(self):
        os.environ["IMAP_GOOGLE_PASS"] = "from-docker"
        acc = config.accounts(config.load(self.cfg_path))[0]
        self.assertEqual(config.credentials(acc, env_path=self.env_path)[1],
                         "from-docker")

    def test_missing_credentials(self):
        acc = config.accounts(config.load(self.cfg_path))[1]
        with self.assertRaises(config.ConfigError) as ctx:
            config.credentials(acc, env_path=self.env_path)
        self.assertIn("IMAP_YANDEX_USER", str(ctx.exception))

    def test_broken_account(self):
        p = Path(self.tmp.name) / "bad.yaml"
        p.write_text("mail:\n  accounts:\n    Broken:\n      env: X\n",
                     encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.accounts(config.load(p))


if __name__ == "__main__":
    unittest.main()
