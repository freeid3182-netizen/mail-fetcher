#!/usr/bin/env python3
"""
AtoZ Commercial — mail fetcher.

The portal's own server cannot open a mailbox: GoDaddy shuts port 993, proved by probing
it from the server itself. This worker does the IMAP part from somewhere that can reach it
(a GitHub Actions runner) and hands what it finds to the portal over ordinary https.

It only ever READS a mailbox and only ever PUSHES to the portal. It never sends a message,
never deletes one, never marks one read in the mailbox — the mailbox is left exactly as it
was, so nothing here can disturb a live business inbox.

Configuration comes from the environment, never from a file in the repository:

  PORTAL_URL  https://atozengineerings.com/billing/api.php
  PUSH_KEY    from the portal: Mailboxes page → worker key
  MAILBOXES   JSON list, one entry per mailbox:
              [{"box":"SHY-Yahoo","host":"imap.mail.yahoo.com","user":"...","pass":"app password"}]

`box` is the label the portal shows. Keep it short and keep it stable: it is half of a
message's id, so renaming a box makes its old messages look new.
"""

import email
import email.header
import email.utils
import imaplib
import json
import os
import sys
import base64
import urllib.request
import urllib.error

# A first run must not drag years of mail into the portal. With nothing seen yet, only
# this many of the newest messages are taken; after that every new one is taken as it
# arrives. The cap per run is separate, so one quiet mailbox cannot starve the others.
FIRST_RUN_TAKE = int(os.environ.get("FIRST_RUN_TAKE", "15"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "40"))
MAX_ATT_BYTES = 4 * 1024 * 1024        # bigger attachments travel as a name only
MAX_ATTS = 12
BODY_CHARS = 40000


def portal(url, key, action, payload=None, timeout=60):
    req = urllib.request.Request(
        url + "?action=" + action,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        # The host's firewall answers "error code: 1010" to Python's own User-Agent —
        # it reads urllib as a bot. A name of our own gets through and is honest about
        # who is calling.
        headers={"X-BL-PUSH": key, "Content-Type": "application/json",
                 "User-Agent": "AtoZ-Mail-Fetcher/1.0"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def dec(raw):
    """A header as a person would read it: RFC 2047 words joined, junk bytes not fatal."""
    if raw is None:
        return ""
    out = []
    for part, enc in email.header.decode_header(str(raw)):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", "replace"))
        else:
            out.append(part)
    return "".join(out).replace("\r", " ").replace("\n", " ").strip()


def body_and_atts(msg):
    """The readable text of a message, plus its attachments.

    Plain text is preferred; HTML is taken only when there is nothing else, tags stripped
    roughly — the portal's reader wants words, not markup.
    """
    text, html, atts = "", "", []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition") or "")
        name = dec(part.get_filename())
        ctype = part.get_content_type()
        if name or "attachment" in disp.lower():
            if len(atts) >= MAX_ATTS:
                continue
            try:
                raw = part.get_payload(decode=True) or b""
            except Exception:
                raw = b""
            atts.append({
                "name": name or "attachment",
                "type": ctype,
                "size": len(raw),
                "b64": base64.b64encode(raw).decode("ascii") if 0 < len(raw) <= MAX_ATT_BYTES else "",
            })
            continue
        try:
            payload = (part.get_payload(decode=True) or b"").decode(
                part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain":
            text += payload + "\n"
        elif ctype == "text/html":
            html += payload + "\n"

    if not text.strip() and html.strip():
        import re
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        t = re.sub(r"(?i)<br\s*/?>|</p>", "\n", t)
        t = re.sub(r"<[^>]+>", " ", t)
        t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
        text = re.sub(r"[ \t]{2,}", " ", t)
    return text[:BODY_CHARS], atts


def fetch_box(cfg, seen_uid):
    """Everything newer than seen_uid in one mailbox, oldest first."""
    box = cfg["box"]
    out, highest = [], seen_uid
    M = imaplib.IMAP4_SSL(cfg.get("host", "imap.mail.yahoo.com"), int(cfg.get("port", 993)))
    try:
        M.login(cfg["user"], cfg["pass"])
        M.select(cfg.get("folder", "INBOX"), readonly=True)   # readonly: nothing is marked read
        typ, data = M.uid("search", None, "ALL")
        if typ != "OK":
            return out, highest, "search failed"
        uids = [int(u) for u in data[0].split()]
        if not uids:
            return out, highest, ""
        if seen_uid <= 0:
            wanted = uids[-FIRST_RUN_TAKE:]
        else:
            wanted = [u for u in uids if u > seen_uid][:MAX_PER_RUN]
        for uid in wanted:
            typ, d = M.uid("fetch", str(uid), "(BODY.PEEK[])")   # PEEK: leaves \Seen alone
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            msg = email.message_from_bytes(d[0][1])
            text, atts = body_and_atts(msg)
            name, addr = email.utils.parseaddr(str(msg.get("From") or ""))
            out.append({
                "box": box, "uid": str(uid),
                "from": dec(name) or addr, "addr": addr,
                "to": dec(msg.get("To")), "subject": dec(msg.get("Subject")),
                "date": dec(msg.get("Date")), "body": text, "atts": atts,
            })
            highest = max(highest, uid)
        return out, highest, ""
    finally:
        try:
            M.logout()
        except Exception:
            pass


def main():
    url = os.environ.get("PORTAL_URL", "").strip()
    key = os.environ.get("PUSH_KEY", "").strip()
    raw = os.environ.get("MAILBOXES", "").strip()
    if not url or not key or not raw:
        print("PORTAL_URL, PUSH_KEY and MAILBOXES must all be set")
        return 2
    try:
        boxes = json.loads(raw)
    except Exception as e:
        print("MAILBOXES is not valid JSON:", e)
        return 2

    try:
        state = portal(url, key, "mail_seen")
    except urllib.error.HTTPError as e:
        print("portal refused the worker key:", e.code, e.read()[:200])
        return 2
    seen = state.get("seen") or {}
    print("portal holds %s message(s); seen: %s" % (state.get("held"), seen))

    total, failed = 0, 0
    for cfg in boxes:
        label = cfg.get("box", "?")
        try:
            msgs, high, err = fetch_box(cfg, int(seen.get(label, 0) or 0))
            if err:
                print("%-14s %s" % (label, err))
                failed += 1
                continue
            if not msgs:
                print("%-14s nothing new" % label)
                continue
            # Pushed in small batches: one fat POST is what shared hosting drops.
            for i in range(0, len(msgs), 5):
                r = portal(url, key, "mail_push", {"msgs": msgs[i:i + 5]}, timeout=120)
                total += int(r.get("added", 0))
            print("%-14s %d new (uid up to %d)" % (label, len(msgs), high))
        except Exception as e:
            # One bad mailbox must not stop the others — a wrong app password is common.
            print("%-14s FAILED: %s" % (label, e))
            failed += 1

    print("added %d message(s); %d mailbox(es) failed" % (total, failed))
    return 1 if failed and total == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
