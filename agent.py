#!/usr/bin/env python3
"""
AtoZ Commercial — the mail agent's driver.

fetch.py brings mail in. This asks the portal to READ it: one HTTP request per message,
because shared hosting kills a long request and because a failure then costs one message
and not a run. Everything that matters happens on the portal — this file holds no
intelligence, no rules and no key to Anthropic. It is a loop and a printer.

Why the loop lives here and not in PHP: a scheduled run may have thirty messages to get
through. In one PHP request that is a guaranteed timeout; in thirty short ones it is thirty
seconds of work that can stop halfway and pick up next time.

Configuration, from the environment, exactly as fetch.py takes it:

  PORTAL_URL   https://atozengineerings.com/billing/api.php
  PUSH_KEY     the same worker key — it opens the mail doors and nothing else
  AGENT_MAX    how many messages to read in one run (default 25)

What the agent is allowed to do is NOT set here. It is an admin setting inside the portal
(Mailboxes → the mail agent): dry, cards, drafts. This driver cannot change it and cannot
override it; it only prints which one is in force.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

MAX_PER_RUN = int(os.environ.get("AGENT_MAX", "25"))
PAUSE = float(os.environ.get("AGENT_PAUSE", "0.4"))


def portal(url, key, action, payload=None, timeout=120):
    # Same two lessons as fetch.py: a CDN in front of the portal will serve an outside
    # caller a cached GET, so every call carries its own query; and the host's firewall
    # answers 1010 to Python's default User-Agent, so the worker gives its name.
    req = urllib.request.Request(
        url + "?action=" + action + "&_=" + str(int(time.time() * 1000)),
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"X-BL-PUSH": key, "Content-Type": "application/json",
                 "User-Agent": "AtoZ-Mail-Agent/1.0"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    url = os.environ.get("PORTAL_URL", "").strip()
    key = os.environ.get("PUSH_KEY", "").strip()
    if not url or not key:
        print("PORTAL_URL and PUSH_KEY must both be set")
        return 2

    try:
        state = portal(url, key, "agent_ping")
    except urllib.error.HTTPError as e:
        print("portal refused the worker key:", e.code, e.read()[:200])
        return 2
    if not state.get("key"):
        print("the portal has no Anthropic key saved — nothing to do")
        return 0
    print("agent v%s · setting: %s · %s held · %s to read"
          % (state.get("ver"), state.get("mode"), state.get("held"), state.get("todo")))

    todo = portal(url, key, "agent_next" + "&limit=%d" % MAX_PER_RUN).get("msgs") or []
    if not todo:
        print("nothing new to read")
        return 0

    spent = 0.0
    counts = {}
    cards = drafts = failed = 0
    for i, m in enumerate(todo, 1):
        subj = (m.get("subject") or "")[:60]
        try:
            r = portal(url, key, "agent_read", {"id": m["id"]})
        except urllib.error.HTTPError as e:
            # One message that cannot be read must not end the run: the next one may be
            # the payment advice somebody is waiting for.
            print("%2d. %-60s FAILED %s %s" % (i, subj, e.code, e.read()[:120]))
            failed += 1
            continue
        except Exception as e:
            print("%2d. %-60s FAILED %s" % (i, subj, e))
            failed += 1
            continue

        if not r.get("ok"):
            print("%2d. %-60s failed: %s" % (i, subj, r.get("error", "?")))
            failed += 1
            continue
        cat = r.get("category", "?")
        counts[cat] = counts.get(cat, 0) + 1
        spent += float(r.get("usd") or 0)
        if r.get("card"):
            cards += 1
        if r.get("draft"):
            drafts += 1
        cache = r.get("cache") or {}
        print("%2d. %-60s %-22s %-6s $%.5f  cache r/w %s/%s %s%s"
              % (i, subj, cat, r.get("confidence", ""), float(r.get("usd") or 0),
                 cache.get("read", 0), cache.get("write", 0),
                 ("→ " + r["card"]) if r.get("card") else "",
                 ("  " + r["draft"]) if r.get("draft") else ""))
        # A shared host answers better when it is not hit flat out.
        time.sleep(PAUSE)

    print("\nread %d message(s), %d failed, $%.4f spent" % (len(todo) - failed, failed, spent))
    if counts:
        print("filed as: " + ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items())))
    if cards or drafts:
        print("%d review card(s), %d draft(s) waiting for a person on the Approvals page" % (cards, drafts))
    else:
        print("nothing was put in front of anyone (this is what the dry setting does)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
