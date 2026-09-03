# -*- coding: utf-8 -*-
"""
IMAP-клиент на стандартной imaplib: соединения, папки, разбор ответов.

Одна сессия на ящик (кэшируется на время работы процесса), переподключение
при обрыве. Письмо адресуется UID внутри «Входящих» — он стабилен, пока
не сменился UIDVALIDITY папки (это отслеживает индекс).

Здесь нет бизнес-логики агента: только «поговорить с сервером и вернуть
питоновские структуры». Инструменты — в tools/mail.py и tools/mail_actions.py.
"""
import base64
import email
import email.policy
import html
import imaplib
import re
import socket
import ssl
import time
from email.header import decode_header, make_header
from email.parser import BytesParser

from . import config
from .log import get as _log

SOCKET_TIMEOUT = 60
HEADER_FIELDS = "(FROM SUBJECT)"
FETCH_HEADERS = f"(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS {HEADER_FIELDS}])"

# Запасные имена папок, если сервер не отдаёт атрибуты SPECIAL-USE (RFC 6154)
TRASH_NAMES = ("Trash", "Deleted Messages", "Deleted Items", "Корзина",
               "[Gmail]/Trash", "[Gmail]/Корзина", "INBOX.Trash")
DRAFTS_NAMES = ("Drafts", "Черновики", "[Gmail]/Drafts", "[Gmail]/Черновики",
                "INBOX.Drafts")


class MailError(Exception):
    """Общая ошибка при обращении к почте (сеть, авторизация, протокол)."""


# ── Modified UTF-7 (RFC 3501, имена папок) ───────────────────────────

def utf7_encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def utf7_decode(s: str) -> str:
    res, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.find("-", i + 1)
            if j < 0:
                res.append(s[i:])
                break
            chunk = s[i + 1:j]
            if not chunk:
                res.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    res.append(base64.b64decode(b64).decode("utf-16-be"))
                except (ValueError, UnicodeDecodeError):
                    res.append(s[i:j + 1])
            i = j + 1
        else:
            res.append(s[i])
            i += 1
    return "".join(res)


def quote_folder(name: str) -> str:
    """Имя папки → аргумент IMAP-команды (UTF-7 + кавычки)."""
    enc = utf7_encode(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{enc}"'


# ── Разбор ответов ───────────────────────────────────────────────────

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.*)$',
                      re.S)


def parse_list_line(item) -> dict:
    """Строка ответа LIST → {"flags": {...}, "delim": "/", "name": "..."}."""
    if isinstance(item, tuple):          # имя пришло литералом
        meta, name_bytes = item[0], item[1]
        m = _LIST_RE.match(meta)
        if not m:
            return None
        name = name_bytes.decode("utf-8", "replace")
    else:
        m = _LIST_RE.match(item)
        if not m:
            return None
        name = m.group("name").decode("utf-8", "replace").strip()
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    flags = {f.decode("ascii", "replace").lower()
             for f in m.group("flags").split() if f}
    delim = m.group("delim").decode("ascii", "replace").strip('"')
    return {"flags": flags, "delim": None if delim == "NIL" else delim,
            "name": utf7_decode(name)}


def group_fetch(data: list) -> list:
    """Ответ FETCH из imaplib → [(meta_bytes, payload_bytes|None)].

    imaplib отдаёт список, где литерал — кортеж (заголовок, тело), а хвост
    атрибутов и «)» — отдельные bytes; некоторые серверы ставят FLAGS
    ПОСЛЕ литерала, поэтому хвост приклеивается к заголовку записи.
    """
    records = []
    for item in data or []:
        if isinstance(item, tuple):
            records.append([item[0] or b"", item[1]])
        elif isinstance(item, bytes):
            if records and item.strip() not in (b")", b""):
                records[-1][0] += b" " + item
            elif not records and item.strip():
                records.append([item, None])
    return [(m, p) for m, p in records]


_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_IDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


def _decode_hdr(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # noqa: BLE001 — кривые заголовки бывают
        return value.strip()


def parse_header_record(meta: bytes, payload: bytes, now: float = None) -> dict:
    """Одна запись FETCH с заголовками → карточка письма (как ждёт индекс).
    Без UID запись бесполезна — возвращается None."""
    m = _UID_RE.search(meta)
    if not m:
        return None
    uid = int(m.group(1))
    fl = _FLAGS_RE.search(meta)
    flags = fl.group(1).decode("ascii", "replace").lower() if fl else ""
    unread = "\\seen" not in flags
    received = None
    d = _IDATE_RE.search(meta)
    if d:
        t = imaplib.Internaldate2tuple(b'INTERNALDATE "' + d.group(1) + b'"')
        if t:
            received = time.mktime(t)
    sender = subject = ""
    if payload:
        try:
            msg = BytesParser(policy=email.policy.compat32).parsebytes(payload)
            sender = _decode_hdr(msg.get("From"))
            subject = _decode_hdr(msg.get("Subject"))
        except Exception:  # noqa: BLE001
            pass
    now = now or time.time()
    age = max(0.0, now - received) if received else 0.0
    return {"id": uid, "unread": unread, "sender": " ".join(sender.split()),
            "subject": " ".join(subject.split()), "age_sec": age,
            "received": received}


def uid_set(ids) -> str:
    """Список UID → строка набора для команд (сжатые диапазоны)."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return ""
    parts, start, prev = [], ids[0], ids[0]
    for u in ids[1:]:
        if u == prev + 1:
            prev = u
            continue
        parts.append(f"{start}:{prev}" if start != prev else str(start))
        start = prev = u
    parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


def parse_search(data: list) -> list:
    out = []
    for item in data or []:
        if isinstance(item, bytes):
            out.extend(int(x) for x in item.split() if x.isdigit())
    return out


# ── Текст письма ────────────────────────────────────────────────────

_STRIP_BLOCKS = re.compile(r"<(script|style|head)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    s = _STRIP_BLOCKS.sub(" ", s or "")
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s, flags=re.I)
    s = _TAGS.sub(" ", s)
    s = html.unescape(s)
    lines = [" ".join(line.split()) for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_text(raw: bytes, max_chars: int = 1500) -> str:
    """RFC822 → текст: text/plain, иначе HTML без тегов. Обрезка по max_chars."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as e:  # noqa: BLE001
        raise MailError(f"не удалось разобрать письмо: {e}")
    text = ""
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is not None:
            content = body.get_content()
            if body.get_content_subtype() == "html":
                content = html_to_text(content)
            text = content
    except Exception as e:  # noqa: BLE001
        _log().debug(f"imap: get_body не удался ({e}), пробую вручную")
    if not text:
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    text = part.get_content()
                    break
                except Exception:  # noqa: BLE001
                    continue
    text = (text or "").strip()
    return text[: int(max_chars)] if max_chars else text


# ── Сессия ──────────────────────────────────────────────────────────

class Session:
    """Соединение с одним ящиком. Все команды — через uid()/select()/cmd()."""

    def __init__(self, account: dict):
        self.account = account
        self.name = account["name"]
        self._conn = None
        self._selected = None      # (folder, readonly)
        self._folders = None
        self._caps = None

    # соединение
    def _connect(self):
        user, password = config.credentials(self.account)
        host, port = self.account["host"], self.account["port"]
        lg = _log()
        t0 = time.monotonic()
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=SOCKET_TIMEOUT,
                                     ssl_context=ssl.create_default_context())
        except (OSError, ssl.SSLError) as e:
            raise MailError(f"{self.name}: не удалось подключиться к {host}:{port} "
                            f"({e})")
        try:
            conn.login(user, password)
        except imaplib.IMAP4.error as e:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
            raise MailError(f"{self.name}: сервер {host} отверг логин/пароль "
                            f"({str(e)[:120]}) — проверьте {self.account['env']}_USER "
                            f"и {self.account['env']}_PASS в .env")
        self._conn = conn
        self._selected = None
        # capabilities после логина шире, чем до (Gmail добавляет MOVE, UIDPLUS…)
        caps = list(conn.capabilities or ())
        try:
            typ, data = conn.capability()
            if typ == "OK":
                for item in data or []:
                    if isinstance(item, bytes):
                        caps.extend(item.split())
        except imaplib.IMAP4.error as e:
            lg.debug(f"imap {self.name}: CAPABILITY после логина: {e}")
        self._caps = {(c.decode("ascii", "replace") if isinstance(c, bytes)
                       else str(c)).upper() for c in caps}
        lg.debug(f"imap {self.name}: подключён к {host} за "
                 f"{time.monotonic() - t0:.2f} с, caps={len(self._caps)}")

    def conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self._connect()
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None
        self._selected = None

    def has_cap(self, cap: str) -> bool:
        self.conn()
        return cap.upper() in (self._caps or set())

    def _call(self, label: str, fn, *args):
        """Выполнить команду; при обрыве соединения переподключиться и повторить."""
        lg = _log()
        for attempt in (1, 2):
            t0 = time.monotonic()
            try:
                typ, data = fn(*args)
            except (imaplib.IMAP4.abort, ConnectionError, socket.timeout,
                    ssl.SSLError, OSError) as e:
                lg.warning(f"imap {self.name}: {label}: обрыв ({e}), "
                           f"попытка {attempt}")
                self.close()
                if attempt == 2:
                    raise MailError(f"{self.name}: связь с сервером потеряна ({e})")
                continue
            except imaplib.IMAP4.error as e:
                raise MailError(f"{self.name}: {label}: {str(e)[:200]}")
            dt = time.monotonic() - t0
            if typ != "OK":
                raise MailError(f"{self.name}: {label}: сервер ответил {typ} "
                                f"{(data[0] if data else b'')[:200]!r}")
            lg.debug(f"imap {self.name}: {label}: {dt:.2f} с OK")
            return data
        return None

    # папки
    def folders(self, refresh: bool = False) -> list:
        """[{"flags", "delim", "name"}] всех папок ящика (кэш)."""
        if self._folders is None or refresh:
            data = self._call("list", lambda: self.conn().list('""', '"*"'))
            out = []
            for item in data or []:
                row = parse_list_line(item)
                if row and row["name"]:
                    out.append(row)
            self._folders = out
        return self._folders

    def selectable_folders(self) -> list:
        return [f["name"] for f in self.folders()
                if "\\noselect" not in f["flags"]]

    def _special(self, attr: str, fallbacks: tuple, override: str) -> str:
        if override:
            return override
        for f in self.folders():
            if attr in f["flags"]:
                return f["name"]
        names = {f["name"].lower(): f["name"] for f in self.folders()}
        for cand in fallbacks:
            if cand.lower() in names:
                return names[cand.lower()]
        raise MailError(f"{self.name}: не нашёл папку {attr[1:]} — задайте её "
                        f"в config.yaml полем {attr[1:]}")

    def trash_folder(self) -> str:
        return self._special("\\trash", TRASH_NAMES, self.account.get("trash"))

    def drafts_folder(self) -> str:
        return self._special("\\drafts", DRAFTS_NAMES, self.account.get("drafts"))

    # выбор папки
    def select(self, folder: str = "INBOX", readonly: bool = True) -> int:
        """Выбрать папку; вернуть число писем (EXISTS)."""
        conn = self.conn()
        data = self._call(f"select {folder}",
                          lambda: conn.select(quote_folder(folder), readonly=readonly))
        self._selected = (folder, readonly)
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def uidvalidity(self, folder: str = "INBOX") -> int:
        """UIDVALIDITY папки: пока он не менялся, UID писем стабильны."""
        conn = self.conn()
        data = self._call(f"status {folder}",
                          lambda: conn.status(quote_folder(folder), "(UIDVALIDITY)"))
        for item in data or []:
            if isinstance(item, tuple):
                item = b" ".join(x for x in item if isinstance(x, bytes))
            m = re.search(rb"UIDVALIDITY (\d+)", item or b"")
            if m:
                return int(m.group(1))
        return 0

    def uid(self, command: str, *args, label: str = None) -> list:
        """UID <command> <args…> в текущей папке."""
        conn = self.conn()
        return self._call(label or f"uid {command}",
                          lambda: conn.uid(command, *args))

    # высокоуровневые чтения
    def all_uids(self, folder: str = "INBOX") -> list:
        """Все UID папки, по возрастанию (старые → новые)."""
        self.select(folder, readonly=True)
        return sorted(parse_search(self.uid("SEARCH", None, "ALL", label="search all")))

    def search_uids(self, criteria: str, folder: str = "INBOX") -> list:
        self.select(folder, readonly=True)
        return sorted(parse_search(self.uid("SEARCH", None, criteria,
                                            label=f"search {criteria}")))

    def fetch_headers(self, uids: list, folder: str = "INBOX") -> list:
        """Карточки писем по списку UID (порядок — от новых к старым)."""
        if not uids:
            return []
        self.select(folder, readonly=True)
        data = self.uid("FETCH", uid_set(uids), FETCH_HEADERS,
                        label=f"fetch headers n{len(uids)}")
        now = time.time()
        rows = []
        for meta, payload in group_fetch(data):
            row = parse_header_record(meta, payload, now)
            if row:
                rows.append(row)
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows

    def fetch_body(self, uid: int, folder: str = "INBOX") -> bytes:
        self.select(folder, readonly=True)
        data = self.uid("FETCH", str(int(uid)), "(BODY.PEEK[])",
                        label=f"fetch body {uid}")
        for meta, payload in group_fetch(data):
            if payload:
                return payload
        raise MailError(f"{self.name}: письмо {uid} не найдено во «Входящих» — "
                        "возможно, удалено или перемещено")

    def existing(self, uids: list, folder: str = "INBOX") -> set:
        """Какие из UID сейчас реально есть в папке."""
        if not uids:
            return set()
        self.select(folder, readonly=True)
        found = parse_search(self.uid("SEARCH", None, f"UID {uid_set(uids)}",
                                      label=f"search uid n{len(uids)}"))
        return set(found)


_sessions = {}


def session(account: dict) -> Session:
    """Сессия ящика по его описанию из config.accounts() (кэш по имени)."""
    s = _sessions.get(account["name"])
    if s is None:
        s = Session(account)
        _sessions[account["name"]] = s
    return s


def close_all():
    for s in list(_sessions.values()):
        s.close()
    _sessions.clear()
