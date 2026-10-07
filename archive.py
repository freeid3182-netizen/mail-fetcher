#!/usr/bin/env python3
"""AtoZ Commercial — the IMAP side of the canonical mail archive (Phase 7A).

fetch.py keeps doing exactly what it always did: newest mail, INBOX only, into the live tray.
It is untouched, because that tray is what the Inbox page reads today and nothing should go
dark while this is proved.

This is the second road. For a mailbox the PORTAL cannot read itself it fills the same
canonical archive the Gmail path fills — Inbox AND Sent, bounded by month, resumable, and
identified by Message-ID rather than by UID.

Why it exists at all: a Google mailbox is read by the portal over ordinary HTTPS, so no
worker is involved. Yahoo publishes no mail REST API to a caller like us — its mail scopes
are not available for self-serve signup, a third party has to apply and be approved — so a
Yahoo mailbox is still read over IMAP, from somewhere that can reach port 993, which this
runner can and the portal cannot.

It only ever READS. Mailboxes are opened read-only and messages are fetched with BODY.PEEK,
so nothing is marked read, moved, deleted or sent. Stop the schedule and the mailbox is
exactly as it was.

What it does NOT decide: which firm a message belongs to, what its canonical id is, or
whether it is new. All three are the portal's, so a bug or a lie here changes nothing.

  PORTAL_URL  https://atozengineerings.com/billing/api.php
  PUSH_KEY    from the portal: Mailboxes page -> worker key
  MAILBOXES   the same JSON list fetch.py uses; `box` matches the adopted legacy label

Usage:  python archive.py            one pass over whatever the portal says is outstanding
"""

import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PORTAL = os.environ.get("PORTAL_URL", "").strip()
KEY = os.environ.get("PUSH_KEY", "").strip()
MONTHS = int(os.environ.get("ARCHIVE_MONTHS", "3"))
# One batch per request. Shared hosting drops a fat POST, and a batch that is too big also
# means more to redo when something fails halfway.
BATCH = int(os.environ.get("ARCHIVE_BATCH", "25"))
# A ceiling per run so one quiet mailbox cannot starve the others and a run always ends.
MAX_PER_RUN = int(os.environ.get("ARCHIVE_MAX_PER_RUN", "400"))
BODY_CHARS = 60000
IMAP_TIMEOUT = 60

MONTH = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def portal(action, payload=None, timeout=120):
    """One call to the portal. A fresh query every time: a CDN in front of this host served a
    cached GET to the worker once already, and it cost a day working out why."""
    req = urllib.request.Request(
        PORTAL + "?action=" + action + "&_=" + str(int(time.time() * 1000)),
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"X-BL-PUSH": KEY, "Content-Type": "application/json",
                 "User-Agent": "AtoZ-Mail-Archive/1.0"},
        method="GET" if payload is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def dec(raw):
    """A header as a person would read it. Junk bytes are not fatal."""
    if raw is None:
        return ""
    out = []
    for part, enc in email.header.decode_header(str(raw)):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", "replace"))
            except (LookupError, TypeError):
                out.append(part.decode("utf-8", "replace"))
        else:
            out.append(part)
    return "".join(out).replace("\r", " ").replace("\n", " ").strip()


def strip_html(h):
    """HTML reduced to TEXT, not sanitised and kept. Email markup is hostile twice over: it
    may carry script, and it may carry words aimed at an AI. Nothing downstream should be
    able to render it, so the tags do not survive this function."""
    t = re.sub(r"(?is)<(script|style|head|svg|iframe|object|embed)\b.*?</\1>", " ", h)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(a, b)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def body_and_atts(msg):
    """The readable text, and attachment METADATA only.

    The bytes are deliberately left in the mailbox. Three months of several mailboxes would
    be a gigabyte of transfer for files nobody has asked to open, and the portal refuses them
    anyway — it stores the name, type and size, and fetches content when somebody asks."""
    text, html, atts, from_html = "", "", [], False
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition") or "")
        name = dec(part.get_filename())
        ctype = part.get_content_type()
        if name or "attachment" in disp.lower():
            if len(atts) >= 25:
                continue
            try:
                raw = part.get_payload(decode=True) or b""
                size = len(raw)
            except Exception:
                size = 0
            atts.append({"name": name or "attachment", "mime": ctype, "size": size})
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
        text = strip_html(html)
        from_html = True
    return text[:BODY_CHARS], atts, from_html


def imap_date(d):
    """YYYY-MM-DD -> the DD-Mon-YYYY that IMAP SEARCH insists on."""
    y, m, day = d.split("-")
    return "%02d-%s-%s" % (int(day), MONTH[int(m) - 1], y)


def pick_folder(M, want):
    """The real folder name for INBOX or SENT on THIS server.

    Never hardcoded: Gmail calls it [Gmail]/Sent Mail, Yahoo calls it Sent, and others differ
    again. The special-use flag is asked for first because it is the server telling us rather
    than us guessing from a name; the names are only a fallback."""
    if want == "INBOX":
        return "INBOX"
    flag = {"SENT": rb"\\Sent"}[want]
    try:
        typ, data = M.list()
    except Exception:
        return None
    names = []
    for row in (data or []):
        if not isinstance(row, bytes):
            continue
        if re.search(flag, row, re.I):
            m = re.search(rb'"([^"]+)"\s*$', row) or re.search(rb'(\S+)\s*$', row)
            if m:
                return m.group(1).decode("utf-8", "replace")
        m = re.search(rb'"([^"]+)"\s*$', row) or re.search(rb'(\S+)\s*$', row)
        if m:
            names.append(m.group(1).decode("utf-8", "replace"))
    for cand in ("Sent", "[Gmail]/Sent Mail", "Sent Items", "Sent Messages", "INBOX.Sent"):
        for n in names:
            if n.lower() == cand.lower():
                return n
    return None


def read_slice(M, cfg, mb, sl, budget):
    """One month of one folder. Returns (rows, uidvalidity, reached_end, discovered)."""
    real = pick_folder(M, sl["folder"])
    if real is None:
        return [], "", True, 0        # no such folder here: the slice has nothing to find
    typ, data = M.select('"%s"' % real, readonly=True)   # readonly: nothing is marked read
    if typ != "OK":
        raise RuntimeError("cannot open %s" % real)

    uv = ""
    try:
        t2, d2 = M.response("UIDVALIDITY")
        if d2 and d2[0]:
            uv = d2[0].decode("ascii", "replace").strip()
    except Exception:
        pass

    # The server does the date bounding, so a month costs one search and not a walk of the
    # mailbox. BEFORE is exclusive, which is why `to` is the first of the NEXT month.
    q = '(SINCE "%s" BEFORE "%s")' % (imap_date(sl["from"]), imap_date(sl["to"]))
    typ, data = M.uid("search", None, q)
    if typ != "OK":
        raise RuntimeError("search refused")
    uids = [int(u) for u in (data[0] or b"").split()]
    discovered = len(uids)

    after = int(sl.get("resume_after_uid") or 0)
    todo = [u for u in uids if u > after]
    todo.sort()
    reached_end = True
    if len(todo) > budget:
        todo = todo[:budget]
        reached_end = False

    rows = []
    for uid in todo:
        typ, d = M.uid("fetch", str(uid), "(BODY.PEEK[])")   # PEEK: leaves \Seen alone
        if typ != "OK" or not d or not isinstance(d[0], tuple):
            continue
        msg = email.message_from_bytes(d[0][1])
        text, atts, from_html = body_and_atts(msg)
        name, addr = email.utils.parseaddr(str(msg.get("From") or ""))
        rows.append({
            "uid": str(uid),
            "message_id": dec(msg.get("Message-ID")),
            "in_reply_to": dec(msg.get("In-Reply-To")),
            "references": dec(msg.get("References")),
            "from_name": dec(name), "from_addr": addr,
            "to": dec(msg.get("To")), "cc": dec(msg.get("Cc")), "bcc": dec(msg.get("Bcc")),
            "subject": dec(msg.get("Subject")), "date": dec(msg.get("Date")),
            "size": len(d[0][1]), "body": text, "from_html": from_html, "atts": atts})
    return rows, uv, reached_end, discovered


def do_mailbox(mb, creds):
    label = mb.get("legacy_box") or mb["email"]
    cfg = creds.get(label)
    if cfg is None:
        print("%-16s no credentials under that label in MAILBOXES — skipped" % label)
        return 0, 1

    done = 0
    budget = MAX_PER_RUN
    M = imaplib.IMAP4_SSL(cfg.get("host", "imap.mail.yahoo.com"),
                          int(cfg.get("port", 993)), timeout=IMAP_TIMEOUT)
    try:
        M.login(cfg["user"], cfg["pass"])
        for sl in mb["slices"]:
            if budget <= 0:
                print("%-16s %s %s — run budget spent, next run continues" % (label, sl["folder"], sl["ym"]))
                break
            try:
                rows, uv, end, disc = read_slice(M, cfg, mb, sl, budget)
            except Exception as e:
                print("%-16s %s %s FAILED: %s" % (label, sl["folder"], sl["ym"], e))
                continue
            budget -= len(rows)

            # Pushed in small batches, and `done` only on the last one — so an interrupted
            # slice stays PARTIAL and the next run picks it up where this stopped.
            if not rows:
                r = portal("mail7_push", {"mailbox_id": mb["mailbox_id"], "folder": sl["folder"],
                                          "ym": sl["ym"], "uidvalidity": uv, "msgs": [],
                                          "discovered": disc, "done": end})
                print("%-16s %s %s — nothing new (%d in the month)" % (label, sl["folder"], sl["ym"], disc))
                continue
            for i in range(0, len(rows), BATCH):
                chunk = rows[i:i + BATCH]
                last = end and (i + BATCH >= len(rows))
                r = portal("mail7_push", {
                    "mailbox_id": mb["mailbox_id"], "folder": sl["folder"], "ym": sl["ym"],
                    "uidvalidity": uv, "msgs": chunk,
                    "discovered": disc if i == 0 else 0, "done": last})
                done += int(r.get("imported", 0))
                if r.get("write_failed"):
                    print("%-16s %s %s — portal could not store %s message(s): %s"
                          % (label, sl["folder"], sl["ym"], r["write_failed"],
                             (r.get("errors") or [""])[0]))
            print("%-16s %s %s — %d read, %d new (%s)"
                  % (label, sl["folder"], sl["ym"], len(rows), done,
                     "complete" if end else "more next run"))
        return done, 0
    finally:
        try:
            M.logout()
        except Exception:
            pass


def main():
    if not PORTAL or not KEY:
        print("PORTAL_URL and PUSH_KEY must both be set")
        return 2
    raw = os.environ.get("MAILBOXES", "").strip()
    if not raw:
        print("MAILBOXES must be set")
        return 2
    try:
        creds = {c["box"]: c for c in json.loads(raw)}
    except Exception as e:
        print("MAILBOXES is not valid JSON:", e)
        return 2

    try:
        plan = portal("mail7_worker_plan&months=%d" % MONTHS)
    except urllib.error.HTTPError as e:
        print("portal refused the worker key:", e.code, e.read()[:200])
        return 2

    boxes = plan.get("mailboxes") or []
    if not boxes:
        print("nothing outstanding. (A mailbox with no firm mapped is left out on purpose.)")
        return 0

    total, failed = 0, 0
    for mb in boxes:
        try:
            n, f = do_mailbox(mb, creds)
            total += n
            failed += f
        except Exception as e:
            # One bad mailbox must not stop the others. A wrong app password is the usual one.
            print("%-16s FAILED: %s" % (mb.get("email", "?"), e))
            failed += 1
    print("archived %d new message(s); %d mailbox(es) failed" % (total, failed))
    return 1 if failed and total == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
