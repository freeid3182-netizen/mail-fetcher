# -*- coding: utf-8 -*-
"""Can this worker actually send through Yahoo?

The portal has said since 7A that Yahoo cannot send, on two grounds: Yahoo's self-serve
OAuth grants a read scope only, and the portal's own host has no outbound SMTP. Both are
true. Neither of them is a statement about THIS machine — the GitHub runner that already
does Yahoo IMAP with an app password — so the claim has never actually been tested where it
would have to work.

This tests it, and sends nothing.

  1. TCP to smtp.mail.yahoo.com on 465 and 587: is the port even reachable from here?
  2. TLS: 465 is implicit TLS, 587 is STARTTLS. Both are tried properly rather than assumed.
  3. AUTH with the app password already in MAILBOXES — the same credential IMAP uses.
  4. A transaction that is OPENED and then RESET: MAIL FROM and RCPT TO addressed to the
     mailbox ITSELF, then RSET. The server says whether it would accept a message from this
     sender to this recipient. DATA is never sent, so no message is ever created. RCPT TO is
     our own address so that even a server which ignored RSET could not put mail in front of
     anybody else.

It prints hosts, ports, response codes and the server's own words. It prints no password and
no app password, ever — the only identity in the output is our own mailbox address.

Usage:  MAILBOXES='[...]' python smtp_probe.py
"""
import json
import os
import re
import smtplib
import socket
import ssl
import sys

HOST = "smtp.mail.yahoo.com"
TIMEOUT = 20


def redact(s):
    """A server's refusal can echo the username. Never the secret."""
    return re.sub(r"(?i)(pass\w*|auth\w*)\s*[:=]\s*\S+", r"\1 <hidden>", str(s))[:300]


def tcp(port):
    try:
        s = socket.create_connection((HOST, port), timeout=TIMEOUT)
        s.close()
        return True, "open"
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def try_smtp(user, password, port):
    """Returns (stage_reached, detail). Stages: tcp, tls, auth, envelope."""
    try:
        if port == 465:
            ctx = ssl.create_default_context()
            srv = smtplib.SMTP_SSL(HOST, port, timeout=TIMEOUT, context=ctx)
        else:
            srv = smtplib.SMTP(HOST, port, timeout=TIMEOUT)
            srv.ehlo()
            if not srv.has_extn("starttls"):
                srv.quit()
                return "tls", "the server does not offer STARTTLS on this port"
            srv.starttls(context=ssl.create_default_context())
        srv.ehlo()
    except Exception as e:
        return "tcp", "%s: %s" % (type(e).__name__, redact(e))

    try:
        srv.login(user, password)
    except smtplib.SMTPAuthenticationError as e:
        srv.close()
        return "tls", "AUTH refused: %s %s" % (e.smtp_code, redact(e.smtp_error))
    except Exception as e:
        srv.close()
        return "tls", "AUTH failed: %s: %s" % (type(e).__name__, redact(e))

    try:
        # Opened and immediately reset. DATA is never sent, so nothing is delivered, and the
        # recipient is our own mailbox so that even a server ignoring RSET could not reach
        # anybody else.
        code_f, msg_f = srv.mail(user)
        code_r, msg_r = srv.rcpt(user)
        srv.rset()
        srv.quit()
        ok = (200 <= code_f < 300) and (200 <= code_r < 300)
        return ("envelope" if ok else "auth"), "MAIL FROM %s %s | RCPT TO %s %s" % (
            code_f, redact(msg_f), code_r, redact(msg_r))
    except Exception as e:
        try:
            srv.close()
        except Exception:
            pass
        return "auth", "envelope failed: %s: %s" % (type(e).__name__, redact(e))


def main():
    raw = os.environ.get("MAILBOXES", "").strip()
    if not raw:
        print("MAILBOXES must be set")
        return 2
    boxes = json.loads(raw)
    if isinstance(boxes, dict):
        boxes = boxes.get("mailboxes", [])
    yahoo = [b for b in boxes if "yahoo" in str(b.get("host", "")).lower()]

    print("=== PORT REACHABILITY FROM THIS WORKER ===")
    reach = {}
    for port in (465, 587):
        ok, why = tcp(port)
        reach[port] = ok
        print("  %s:%-4d %s   %s" % (HOST, port, "OPEN" if ok else "SHUT", "" if ok else why))

    print()
    print("=== AUTHENTICATION AND ENVELOPE (nothing is sent) ===")
    if not yahoo:
        print("  no Yahoo mailbox in MAILBOXES")
        return 3

    best = {}
    for b in yahoo:
        user = str(b.get("user", ""))
        pw = str(b.get("pass", ""))
        for port in (465, 587):
            if not reach[port]:
                print("  %-38s :%-4d skipped, the port is not reachable" % (user, port))
                continue
            stage, detail = try_smtp(user, pw, port)
            print("  %-38s :%-4d reached=%-9s %s" % (user, port, stage, detail))
            best[user] = max(best.get(user, ""), stage,
                             key=lambda s: ["", "tcp", "tls", "auth", "envelope"].index(s))

    print()
    print("=== VERDICT ===")
    sendable = [u for u, s in best.items() if s == "envelope"]
    if sendable:
        print("  Yahoo SMTP IS usable from this worker for %d mailbox(es)." % len(sendable))
        print("  Authentication succeeded and the server accepted an envelope.")
    else:
        print("  Yahoo SMTP is NOT usable from this worker.")
        print("  Furthest stage reached per mailbox: " + json.dumps(best))
    return 0


if __name__ == "__main__":
    sys.exit(main())
