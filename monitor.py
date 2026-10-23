#!/usr/bin/env python3
"""
AtoZ Commercial — the thing that finally runs the monitor.

`action=mon_sweep` has existed on the portal since Stage C step 4. The Phase 9 audit grepped
both repositories for it and found NO CALLER: the alert engine — overdue money, finance
exceptions the books cannot answer for themselves, tracked work past its check date — had
never swept on a schedule in its life. Everything it needs was built and nothing ever rang
the bell.

This rings it. It is deliberately the smallest possible file:

  * it holds no rules, no thresholds and no decisions. Which findings exist, who may see
    them, whether it is working hours, whether today's summary has gone out, how many
    messages a day are allowed and whether a line is still true a second before it is sent
    are ALL decided inside the portal. Putting any of that here would be a second set of
    rules to disagree with the first.
  * it runs inside the mail workflow that already runs every fifteen minutes, with the key
    and the URL that workflow already has. No new schedule, no new secret, no VPS. The
    comment beside mon_sweep in api.php anticipated exactly this.
  * a bad run must never fail the workflow it is a passenger in. Mail is the workflow's job;
    this is a guest, and a guest that breaks the house is worse than a guest that stays home.

Configuration, from the environment, exactly as fetch.py and agent.py take it:

  PORTAL_URL   https://atozengineerings.com/billing/api.php
  PUSH_KEY     the same worker key — it opens the mail doors and the sweep, and nothing else
  MON_ONLY     optional: sweep one detector only (overdue, exceptions, tasks)

WHAT IT PRINTS, and why it prints it. A log is a place other people read, so no business
figure and no client name goes into it: only counts, states and reasons. "sent 1" means
Telegram ACCEPTED one message. It does not mean anybody read it, and this file will not say
delivered.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


def portal(url, key, action, timeout=180):
    # The same two lessons as fetch.py and agent.py, and they were each learned the hard
    # way: a Cloudflare cache sits in front of this portal and will serve an outside caller
    # a copy up to 31 days old unless every call carries its own query; and the host's
    # firewall reads Python's default User-Agent as a bot and answers 403.
    req = urllib.request.Request(
        url + "?action=" + action + "&cb=" + str(int(time.time() * 1000)),
        headers={"X-BL-PUSH": key, "User-Agent": "AtoZ-Monitor/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    url = os.environ.get("PORTAL_URL", "").strip()
    key = os.environ.get("PUSH_KEY", "").strip()
    if not url or not key:
        print("monitor: PORTAL_URL and PUSH_KEY must both be set — skipped")
        return 0

    only = "".join(c for c in os.environ.get("MON_ONLY", "").strip().lower()
                   if c.isalpha())
    action = "mon_sweep" + ("&only=" + only if only else "")

    try:
        r = portal(url, key, action)
    except urllib.error.HTTPError as e:
        # 401 here means the push key is wrong, which is worth saying loudly and is still
        # not a reason to fail the mail run.
        print("monitor: the portal refused the sweep: %s %s"
              % (e.code, e.read()[:200].decode("utf-8", "replace")))
        return 0
    except Exception as e:
        print("monitor: could not reach the portal: %s" % e)
        return 0

    if not isinstance(r, dict) or not r.get("ok"):
        print("monitor: the sweep did not report ok: %s" % json.dumps(r)[:300])
        return 0

    d = r.get("delivery") or {}
    print("monitor: %s new, %s already known, %s resolved, %s open"
          % (r.get("new"), r.get("already_known"), r.get("resolved"), r.get("open")))

    # A DETECTOR THAT THREW IS NOT A QUIET DAY. One detector failing leaves the others'
    # findings true and the sweep correctly carries on — but a run that printed "0 new" and
    # nothing else would read as "nothing to report" when it means "one engine is broken".
    errs = r.get("errors") or []
    if errs:
        print("monitor: A DETECTOR FAILED, so the list is PARTIAL rather than complete:")
        for e in errs[:5]:
            print("  - %s" % str(e)[:200])

    if d.get("sent"):
        print("monitor: %s message(s) ACCEPTED by Telegram for %s recipient(s)%s"
              % (d.get("sent"), d.get("recipients"),
                 " — today's daily summary" if d.get("summary") else ""))
        print("monitor: accepted is not read. Telegram cannot tell us anybody read it.")
    else:
        print("monitor: nothing sent (%s)" % (d.get("why") or "nothing was due"))

    if d.get("repeats_included"):
        print("monitor: %s of those lines were a repeat of something still unresolved"
              % d.get("repeats_included"))
    if d.get("summary_missed"):
        print("monitor: today's daily summary window was missed — %s" % d.get("summary_missed"))
    if d.get("needs_review"):
        print("monitor: %s earlier notification(s) could not be confirmed as sent and are "
              "waiting for a person. They are NOT being resent: an ambiguous result resent "
              "is how one alert becomes four." % d.get("needs_review"))
    for o in (d.get("outcomes") or []):
        if str(o.get("state")) != "ACCEPTED":
            print("monitor: a message was not accepted (%s, attempt %s): %s"
                  % (o.get("state"), o.get("attempts"), str(o.get("why"))[:160]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
