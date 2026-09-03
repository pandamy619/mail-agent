# -*- coding: utf-8 -*-
"""Тесты чистых функций IMAP-клиента (без сети)."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import imap_client as ic  # noqa: E402


class Utf7Test(unittest.TestCase):
    def test_ascii_unchanged(self):
        self.assertEqual(ic.utf7_encode("INBOX/Sub"), "INBOX/Sub")
        self.assertEqual(ic.utf7_decode("INBOX/Sub"), "INBOX/Sub")

    def test_ampersand(self):
        self.assertEqual(ic.utf7_encode("A&B"), "A&-B")
        self.assertEqual(ic.utf7_decode("A&-B"), "A&B")

    def test_cyrillic_roundtrip(self):
        for name in ("Корзина", "Черновики", "Рассылки 2026", "Входящие/Банк"):
            enc = ic.utf7_encode(name)
            self.assertTrue(enc.isascii(), enc)
            self.assertEqual(ic.utf7_decode(enc), name)

    def test_known_encoding(self):
        # пример из RFC 3501 / практики: «Отправленные» у Яндекса
        self.assertEqual(ic.utf7_decode("&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"),
                         "Отправленные")

    def test_quote_folder(self):
        self.assertEqual(ic.quote_folder("[Gmail]/Trash"), '"[Gmail]/Trash"')
        self.assertEqual(ic.quote_folder('a"b'), '"a\\"b"')


class ListParseTest(unittest.TestCase):
    def test_gmail_style(self):
        row = ic.parse_list_line(b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"')
        self.assertEqual(row["name"], "[Gmail]/Trash")
        self.assertIn("\\trash", row["flags"])
        self.assertEqual(row["delim"], "/")

    def test_unquoted_inbox(self):
        row = ic.parse_list_line(b'(\\HasNoChildren) "/" INBOX')
        self.assertEqual(row["name"], "INBOX")

    def test_utf7_name(self):
        row = ic.parse_list_line(b'(\\HasNoChildren) "|" "&BBoEPgRABDcEOAQ9BDA-"')
        self.assertEqual(row["name"], "Корзина")

    def test_literal_name(self):
        row = ic.parse_list_line((b'(\\Noselect) "/" {5}', b"Fold5"))
        self.assertEqual(row["name"], "Fold5")
        self.assertIn("\\noselect", row["flags"])

    def test_garbage(self):
        self.assertIsNone(ic.parse_list_line(b"not a list line"))


class FetchParseTest(unittest.TestCase):
    HDR = (b"From: =?UTF-8?B?0JjQstCw0L0g0J/QtdGC0YDQvtCy?= <ivan@example.com>\r\n"
           b"Subject: =?utf-8?q?=D0=A1=D1=80=D0=BE=D1=87=D0=BD=D0=BE?= deadline\r\n"
           b"\r\n")

    def test_group_flags_before_literal(self):
        data = [(b'1 (UID 42 FLAGS (\\Seen) INTERNALDATE "03-Sep-2026 10:00:00 +0300" '
                 b'BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}' % len(self.HDR), self.HDR),
                b")"]
        recs = ic.group_fetch(data)
        self.assertEqual(len(recs), 1)
        row = ic.parse_header_record(*recs[0], now=time.time())
        self.assertEqual(row["id"], 42)
        self.assertFalse(row["unread"])
        self.assertEqual(row["sender"], "Иван Петров <ivan@example.com>")
        self.assertEqual(row["subject"], "Срочно deadline")
        self.assertGreater(row["received"], 0)

    def test_group_flags_after_literal(self):
        data = [(b'2 (UID 7 BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}' % len(self.HDR),
                 self.HDR),
                b' FLAGS () INTERNALDATE "01-Jan-2026 00:00:00 +0000")']
        recs = ic.group_fetch(data)
        self.assertEqual(len(recs), 1)
        row = ic.parse_header_record(*recs[0])
        self.assertEqual(row["id"], 7)
        self.assertTrue(row["unread"])

    def test_multiple_records(self):
        data = [(b'1 (UID 1 FLAGS () INTERNALDATE "01-Jan-2026 00:00:00 +0000" BODY[HEADER.FIELDS (FROM SUBJECT)] {4}', b"\r\n\r\n"),
                b")",
                (b'2 (UID 2 FLAGS (\\Seen) INTERNALDATE "02-Jan-2026 00:00:00 +0000" BODY[HEADER.FIELDS (FROM SUBJECT)] {4}', b"\r\n\r\n"),
                b")"]
        rows = [ic.parse_header_record(m, p) for m, p in ic.group_fetch(data)]
        self.assertEqual([r["id"] for r in rows], [1, 2])
        self.assertEqual([r["unread"] for r in rows], [True, False])

    def test_no_uid_is_skipped(self):
        self.assertIsNone(ic.parse_header_record(b"1 (FLAGS ())", b""))


class HelpersTest(unittest.TestCase):
    def test_uid_set_ranges(self):
        self.assertEqual(ic.uid_set([5, 1, 2, 3, 9, 10]), "1:3,5,9:10")
        self.assertEqual(ic.uid_set([]), "")
        self.assertEqual(ic.uid_set([7, 7]), "7")

    def test_parse_search(self):
        self.assertEqual(ic.parse_search([b"3 1 2"]), [3, 1, 2])
        self.assertEqual(ic.parse_search([b""]), [])
        self.assertEqual(ic.parse_search([None]), [])

    def test_html_to_text(self):
        src = ("<html><head><style>p{}</style></head><body><p>Привет,&nbsp;мир</p>"
               "<script>x()</script><div>строка&amp;2</div></body></html>")
        self.assertEqual(ic.html_to_text(src), "Привет, мир\nстрока&2")

    def test_extract_text_plain(self):
        raw = (b"From: a@b\r\nSubject: t\r\nContent-Type: text/plain; charset=utf-8\r\n"
               b"\r\n" + "Тело письма".encode("utf-8"))
        self.assertEqual(ic.extract_text(raw), "Тело письма")

    def test_extract_text_html_only(self):
        raw = (b"From: a@b\r\nSubject: t\r\nContent-Type: text/html; charset=utf-8\r\n"
               b"\r\n<p>Hello <b>world</b></p>")
        self.assertEqual(ic.extract_text(raw), "Hello world")

    def test_extract_text_multipart_prefers_plain(self):
        raw = (b"From: a@b\r\nSubject: t\r\n"
               b"Content-Type: multipart/alternative; boundary=XX\r\n\r\n"
               b"--XX\r\nContent-Type: text/plain\r\n\r\nplain body\r\n"
               b"--XX\r\nContent-Type: text/html\r\n\r\n<p>html body</p>\r\n--XX--\r\n")
        self.assertEqual(ic.extract_text(raw), "plain body")

    def test_extract_text_truncates(self):
        raw = b"From: a@b\r\nContent-Type: text/plain\r\n\r\n" + b"x" * 100
        self.assertEqual(len(ic.extract_text(raw, max_chars=10)), 10)


if __name__ == "__main__":
    unittest.main()
