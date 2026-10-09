# -*- coding: utf-8 -*-
"""Carry the portal's approved outbox over Yahoo SMTP.

This script decides nothing. It asks the portal for messages that are ALREADY QUEUED —
already drafted, already approved by a named person against a hash of their exact words,
already past the firm and mailbox checks, already holding an idempotency key — and it puts
the bytes it is handed on the wire unchanged. Then it says what the server replied.

WHY IT EXISTS AT ALL. The portal has no outbound SMTP, the same reason it has no IMAP. A
probe from this runner found smtp.mail.yahoo.com open on 465 and 587, the app password it
already uses for reading accepted, and an envelope accepted. So Yahoo sends the way Yahoo
reads: from here.

WHAT IT MUST NOT DO, and does not:
  - alter the bytes. The approval is a hash of them, so a changed byte is an unapproved
    message. They go out exactly as received.
  - send twice. Every message is reported back before the next is attempted, and the portal
    refuses a second result for a settled send.
  - invent a recipient. The envelope comes from the portal, not from parsing the message.

  MAILBOXES   the same JSON list the other scripts use; `legacy_box` matches `box`
  PORTAL_URL  https://.../billing/api.php
  PUSH_KEY    the worker's key
"""
import base64
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PORTS = (465, 587)


def smtp_host(imap_host):
    """The SMTP host that goes with a mailbox's IMAP host. Hardcoding Yahoo's was wrong the
    moment a second kind of app-password mailbox existed: the worker authenticates to
    whatever host its own credentials name, so the host is derived from the credentials
    rather than assumed."""
    h = str(imap_host or "").strip().lower()
    if h.startswith("imap."):
        return "smtp." + h[5:]
    return h or "smtp.mail.yahoo.com"
TIMEOUT = 45


def api(url, key, action, body=None, **qs):
    q = urllib.parse.urlencode(dict(action=action, **qs))
    req = urllib.request.Request(url + "?" + q, method="POST" if body is not None else "GET")
    req.add_header("X-BL-PUSH", key)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def creds(boxes, label, email):
    """By label first, because that is what the portal hands over; by address as a fallback,
    because a mailbox adopted under one label and renamed later still has one address."""
    for b in boxes:
        if str(b.get("box", "")) == label:
            return b
    for b in boxes:
        if str(b.get("user", "")).lower() == str(email).lower():
            return b
    return None


def deliver(host, user, password, sender, rcpts, raw):
    """Returns (ok, detail). Tries 465, then 587: a runner's egress can differ by port and
    there is no reason to fail on the first one when the second is standing right there."""
    last = ""
    for port in PORTS:
        try:
            if port == 465:
                srv = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                       context=ssl.create_default_context())
            else:
                srv = smtplib.SMTP(host, port, timeout=TIMEOUT)
                srv.ehlo()
                srv.starttls(context=ssl.create_default_context())
            srv.ehlo()
            srv.login(user, password)
            # The bytes are sent as given. sendmail() would re-encode; send_message() would
            # re-build headers. Neither is allowed to touch an approved message.
            refused = srv.sendmail(sender, rcpts, raw)
            srv.quit()
            if refused:
                return False, "the server refused %d recipient(s) on %d" % (len(refused), port)
            return True, "accepted by %s on %d" % (host, port)
        except Exception as e:
            last = "%s on %d: %s" % (type(e).__name__, port, str(e)[:160])
            try:
                srv.close()
            except Exception:
                pass
    return False, last


def main():
    url = os.environ.get("PORTAL_URL", "").strip()
    key = os.environ.get("PUSH_KEY", "").strip()
    raw_boxes = os.environ.get("MAILBOXES", "").strip()
    if not url or not key or not raw_boxes:
        print("PORTAL_URL, PUSH_KEY and MAILBOXES must all be set")
        return 2
    boxes = json.loads(raw_boxes)
    if isinstance(boxes, dict):
        boxes = boxes.get("mailboxes", [])

    out = api(url, key, "mail7_send_outbox", limit=int(os.environ.get("SEND_BATCH", "5")))
    msgs = out.get("messages", [])
    print("outbox: %d queued, %d unsendable" % (len(msgs), out.get("unsendable", 0)))
    if not msgs:
        print("nothing to send")
        return 0

    sent = failed = 0
    for m in msgs:
        sid = m["send_id"]
        c = creds(boxes, m.get("legacy_box", ""), m.get("email", ""))
        if c is None:
            # Reported as a failure rather than left in the queue for ever. A message nobody
            # can send is a thing somebody needs to be told about.
            api(url, key, "mail7_send_done", {"send_id": sid, "ok": False,
                "error": "the worker holds no credentials for that mailbox"})
            print("  %s  NO CREDENTIALS" % sid)
            failed += 1
            continue
        data = base64.b64decode(m["raw_b64"])
        host = smtp_host(c.get("host"))
        ok, detail = deliver(host, str(c["user"]), str(c["pass"]),
                             m["email"], m["envelope_to"], data)
        # Reported BEFORE the next message is attempted, so a crash leaves at most one
        # message whose outcome is unknown rather than a batch of them.
        r = api(url, key, "mail7_send_done",
                {"send_id": sid, "ok": ok,
                 "provider_message_id": ("smtp:" + detail) if ok else "",
                 "error": "" if ok else detail})
        print("  %s  %s  %s%s" % (sid, "SENT" if ok else "FAILED", detail,
                                  "  (already settled)" if r.get("already") else ""))
        sent += 1 if ok else 0
        failed += 0 if ok else 1

    print("sent %d, failed %d" % (sent, failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
