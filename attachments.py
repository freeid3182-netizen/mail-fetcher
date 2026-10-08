#!/usr/bin/env python3
"""AtoZ Commercial — read what is inside the attachments (Phase 7).

The intelligence pass found 72 payment advices and matched 70 of them to nothing. Not a weak
matcher: "Payment advice 0340001109" from JK Cement quotes JK CEMENT'S OWN advice number, and
our bill number is inside the attached PDF.

WHY THIS RUNS HERE. The portal has no PDF library and no OCR, and putting either on shared
hosting would be slow, fragile, and a new place for a stranger's file to do something
unexpected. Phase 5I(C) already answered this once — PyMuPDF for a text layer, RapidOCR
offline for a scan, run off the server — and this is the same toolchain on the same road the
IMAP worker already travels. It sends back TEXT. The bytes of a stranger's PDF are never
parsed by the portal.

A TEXT LAYER IS NOT OCR, and the two are reported separately. A text layer is what the
document says. OCR is a reading of a picture of what it says, and it is wrong sometimes. The
portal records which engine read each file so a match can be weighed accordingly.

  PORTAL_URL  https://atozengineerings.com/billing/api.php
  PUSH_KEY    from the portal: Mailboxes page -> worker key
  MAILBOXES   the same JSON list the other two scripts use

Usage:  python attachments.py            one bounded pass
"""

import base64
import email
import imaplib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORTAL = os.environ.get("PORTAL_URL", "").strip()
KEY = os.environ.get("PUSH_KEY", "").strip()
BATCH = int(os.environ.get("ATT_BATCH", "40"))
MAX_PAGES = int(os.environ.get("ATT_MAX_PAGES", "25"))
MAX_CHARS = 120000
# OCR is slow and a backfill is not in a hurry. A pass does a bounded number of scans and the
# next pass continues; nothing is lost by stopping.
MAX_OCR = int(os.environ.get("ATT_MAX_OCR", "12"))

_ocr = [None]


def portal(action, payload=None, timeout=180):
    req = urllib.request.Request(
        PORTAL + "?action=" + action + "&_=" + str(int(time.time() * 1000)),
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"X-BL-PUSH": KEY, "Content-Type": "application/json",
                 "User-Agent": "AtoZ-Attachments/1.0"},
        method="GET" if payload is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def say(cid, i, ok, name, mime, data=b"", text="", pages=0, engine="", why=""):
    import hashlib
    body = {"cid": cid, "i": i, "ok": bool(ok), "name": name, "mime": mime,
            "bytes": len(data), "pages": pages, "engine": engine, "why": why,
            "sha256": hashlib.sha256(data).hexdigest() if data else "",
            "text": text[:MAX_CHARS]}
    return portal("mail7_att_text", body)


# ---------------------------------------------------------------- extraction
def pdf_text(data, allow_ocr):
    """A PDF's own text layer first; OCR only for the pages that have none.

    Most business paper that matters here — a payment advice out of SAP, a tax invoice, a
    purchase order — is generated, not scanned, and carries a perfectly good text layer. OCR
    is for the ones that were put through a photocopier, and it is slow enough that it is
    worth not doing when it is not needed.
    """
    try:
        import fitz                                  # PyMuPDF, as Phase 5I(C) used
    except ImportError:
        return None, 0, "", "PyMuPDF is not installed on this runner"
    out, pages, used_ocr, blank = [], 0, False, 0
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            n = min(len(doc), MAX_PAGES)
            pages = len(doc)
            for i in range(n):
                t = doc[i].get_text("text") or ""
                if t.strip():
                    out.append(t)
                    continue
                blank += 1
                if not allow_ocr or blank > MAX_OCR:
                    continue
                o = ocr_page(doc[i])
                if o:
                    out.append(o)
                    used_ocr = True
    except Exception as e:
        return None, 0, "", "the file could not be opened as a PDF: %s" % str(e)[:120]
    txt = "\n".join(out).strip()
    engine = "pymupdf+rapidocr" if used_ocr else "pymupdf"
    if not txt:
        return "", pages, engine, ("the pages carry no text layer and OCR produced nothing"
                                   if blank else "the file carries no text")
    return txt, pages, engine, ""


def ocr_page(page):
    try:
        if _ocr[0] is None:
            from rapidocr_onnxruntime import RapidOCR    # offline, as Phase 5I(C) used
            _ocr[0] = RapidOCR()
        import numpy as np
        from PIL import Image
        pm = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pm.tobytes("png"))).convert("RGB")
        res, _ = _ocr[0](np.array(img))
        if not res:
            return ""
        return "\n".join(r[1] for r in res if len(r) > 1)
    except Exception:
        return ""


def image_text(data):
    try:
        if _ocr[0] is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr[0] = RapidOCR()
        import numpy as np
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        res, _ = _ocr[0](np.array(img))
        if not res:
            return "", "rapidocr", "the picture carries no readable text"
        return "\n".join(r[1] for r in res if len(r) > 1), "rapidocr", ""
    except ImportError:
        return None, "", "RapidOCR or Pillow is not installed on this runner"
    except Exception as e:
        return None, "", "the picture could not be read: %s" % str(e)[:120]


def sheet_text(data, name):
    """A spreadsheet, as its cells read. Numbers are what we are looking for."""
    low = name.lower()
    if low.endswith(".csv"):
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return data.decode(enc), "csv", ""
            except UnicodeDecodeError:
                continue
        return None, "", "the CSV is in an encoding this runner could not read"
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None, "", "openpyxl is not installed on this runner"
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets[:8]:
            for row in ws.iter_rows(max_row=400, values_only=True):
                cells = [str(c) for c in row if c not in (None, "")]
                if cells:
                    out.append(" | ".join(cells))
        wb.close()
        return "\n".join(out), "openpyxl", ""
    except Exception as e:
        return None, "", "the workbook could not be opened: %s" % str(e)[:120]


def plain_text(data):
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc), "text", ""
        except UnicodeDecodeError:
            continue
    return None, "", "the text file is in an encoding this runner could not read"


def extract(kind, data, name):
    if kind == "pdf":
        t, pages, engine, why = pdf_text(data, allow_ocr=True)
        return t, pages, engine, why
    if kind == "image":
        t, engine, why = image_text(data)
        return t, 1, engine, why
    if kind == "sheet":
        t, engine, why = sheet_text(data, name)
        return t, 0, engine, why
    if kind == "text":
        t, engine, why = plain_text(data)
        return t, 0, engine, why
    return None, 0, "", "this kind of file is not one this runner reads"


# ---------------------------------------------------------------- getting the bytes
def bytes_from_portal(a):
    """Gmail is read by the portal over OAuth, so it fetches and hands the file over."""
    r = portal("mail7_att_bytes&cid=%s&i=%d&ym=%s"
               % (urllib.parse.quote(a["cid"]), int(a["i"]), urllib.parse.quote(a["ym"])))
    if not r.get("ok"):
        return None, r.get("error", "the portal would not hand over the bytes")
    return base64.b64decode(r["bytes"]), ""


def bytes_from_imap(a, creds, conns):
    """Yahoo is IMAP, and the worker is the only thing that can reach it."""
    label = a.get("legacy_box") or ""
    cfg = creds.get(label)
    if cfg is None:
        for c in creds.values():
            if c["user"].lower() == str(a.get("email", "")).lower():
                cfg = c
                break
    if cfg is None:
        return None, "no credentials for that mailbox in MAILBOXES"
    key = cfg["user"]
    if key not in conns:
        M = imaplib.IMAP4_SSL(cfg.get("host", "imap.mail.yahoo.com"),
                              int(cfg.get("port", 993)), timeout=60)
        M.login(cfg["user"], cfg["pass"])
        conns[key] = M
    M = conns[key]
    folder = "INBOX" if a["folder"] == "INBOX" else a["folder"]
    typ, _ = M.select('"%s"' % folder, readonly=True)
    if typ != "OK":
        return None, "could not open %s" % folder
    typ, d = M.uid("fetch", str(a["uid"]), "(BODY.PEEK[])")
    if typ != "OK" or not d or not isinstance(d[0], tuple):
        return None, "the message is no longer at that UID"
    msg = email.message_from_bytes(d[0][1])
    n = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = part.get_filename()
        if not fn and "attachment" not in str(part.get("Content-Disposition") or "").lower():
            continue
        if n == int(a["i"]):
            return (part.get_payload(decode=True) or b""), ""
        n += 1
    return None, "that attachment is no longer on the message"


def main():
    if not PORTAL or not KEY:
        print("PORTAL_URL and PUSH_KEY must both be set")
        return 2
    creds = {}
    raw = os.environ.get("MAILBOXES", "").strip()
    if raw:
        try:
            creds = {c["box"]: c for c in json.loads(raw)}
        except Exception as e:
            print("MAILBOXES is not valid JSON:", e)

    try:
        p = portal("mail7_att_pending&limit=%d" % BATCH)
    except urllib.error.HTTPError as e:
        print("portal refused the worker key:", e.code, e.read()[:200])
        return 2
    todo = p.get("pending") or []
    if not todo:
        print("nothing to read.", json.dumps(p.get("skipped") or {}))
        return 0

    conns = {}
    read = failed = 0
    try:
        for a in todo:
            name = a.get("name", "file")
            try:
                if a["provider"] == "google":
                    data, why = bytes_from_portal(a)
                else:
                    data, why = bytes_from_imap(a, creds, conns)
            except Exception as e:
                data, why = None, "fetching failed: %s" % str(e)[:140]
            if data is None or not data:
                say(a["cid"], a["i"], False, name, a.get("mime", ""),
                    why=why or "no bytes came back")
                failed += 1
                print("  %-44s FETCH FAILED  %s" % (name[:44], (why or "")[:60]))
                continue
            text, pages, engine, why = extract(a["kind"], data, name)
            if text is None:
                say(a["cid"], a["i"], False, name, a.get("mime", ""), data=data, why=why)
                failed += 1
                print("  %-44s NOT READ      %s" % (name[:44], why[:60]))
                continue
            r = say(a["cid"], a["i"], True, name, a.get("mime", ""), data=data,
                    text=text, pages=pages, engine=engine, why=why)
            if r.get("read"):
                read += 1
                print("  %-44s %-16s %5d chars  %d page(s)"
                      % (name[:44], engine, r.get("chars", 0), pages))
            else:
                failed += 1
                print("  %-44s EMPTY         %s" % (name[:44], why[:60]))
    finally:
        for M in conns.values():
            try:
                M.logout()
            except Exception:
                pass

    print("read %d, could not read %d, of %d offered" % (read, failed, len(todo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
