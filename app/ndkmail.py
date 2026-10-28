"""
Електронни фактури на НДК от пощата invoices@trista.bg (IMAP).
Четем фактурата по НДК код и я сравняваме с осчетоводеното в Мистрал (OCR).
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from email.header import decode_header, make_header


def _conn():
    host = os.getenv("IMAP_HOST", "")
    user = os.getenv("IMAP_USER", "")
    pw = os.getenv("IMAP_PASS", "")
    if not (host and user and pw):
        raise RuntimeError("IMAP не е настроен")
    M = imaplib.IMAP4_SSL(host, int(os.getenv("IMAP_PORT", "993")))
    M.login(user, pw)
    return M


def _hdr(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def probe(limit: int = 30, days: int = 7) -> dict:
    """Какво има в пощата: от кого, тема, прикачени файлове (име, тип, размер)."""
    import datetime as dt
    M = _conn()
    try:
        M.select("INBOX", readonly=True)
        since = (dt.date.today() - dt.timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split()
        out = []
        for i in ids[-limit:][::-1]:
            typ, msg_data = M.fetch(i, "(BODY.PEEK[HEADER] BODYSTRUCTURE)")
            raw = b"".join(p[1] for p in msg_data if isinstance(p, tuple))
            msg = email.message_from_bytes(raw)
            typ2, full = M.fetch(i, "(BODY.PEEK[])")
            m2 = email.message_from_bytes(b"".join(p[1] for p in full if isinstance(p, tuple)))
            atts = []
            for part in m2.walk():
                fn = part.get_filename()
                if fn:
                    payload = part.get_payload(decode=True) or b""
                    atts.append({"name": _hdr(fn), "type": part.get_content_type(), "bytes": len(payload)})
            out.append({"uid": i.decode(), "from": _hdr(msg.get("From")), "subject": _hdr(msg.get("Subject")),
                        "date": msg.get("Date"), "attachments": atts})
        return {"total_since": len(ids), "messages": out}
    finally:
        M.logout()


def fetch_attachment(uid: str, name_part: str) -> tuple[str, bytes]:
    M = _conn()
    try:
        M.select("INBOX", readonly=True)
        typ, full = M.fetch(uid.encode(), "(BODY.PEEK[])")
        m2 = email.message_from_bytes(b"".join(p[1] for p in full if isinstance(p, tuple)))
        for part in m2.walk():
            fn = _hdr(part.get_filename())
            if fn and name_part.lower() in fn.lower():
                return fn, part.get_payload(decode=True) or b""
        raise FileNotFoundError(name_part)
    finally:
        M.logout()
