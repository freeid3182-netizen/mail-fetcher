# -*- coding: utf-8 -*-
"""Phase 13 Step 9G — the portal client. The worker's only way to say anything.

THE PORTAL IS THE AUTHORITY AND THIS FILE IS A MOUTH, NOT A BRAIN. Every refusal that matters
lives in api.php: whether a task may be claimed, whether an approval is still an approval of
this content, whether an idempotency key has been spent. This file asks and reports, and it
does not interpret an answer into permission. If the portal says no, the answer is no -- there
is no retry-with-different-wording anywhere in it.

TWO THINGS THAT ARE NOT OBVIOUS AND WERE BOTH LEARNED THE HARD WAY HERE:

  * THE USER AGENT. The host's firewall answers python-urllib's default agent with 403. A
    worker that cannot be told apart from a scanner gets treated as one.
  * THE CACHE. A Cloudflare cache sits in front of api.php, so every GET carries a cache
    buster. Without it a worker can poll a list of tasks that is weeks old.

The push key arrives as an environment secret, exactly as the mail worker's does, and it is
never logged -- the vault redacts it, and this file never prints a response body on its own.
"""
import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request

UA = "AtoZ-Browser-Worker/1.0 (+portal automation; contact the portal owner)"
TIMEOUT = 45


class PortalError(RuntimeError):
    pass


class Portal(object):
    def __init__(self, base_url, push_key, worker, location, vault=None, opener=None):
        if not base_url:
            raise PortalError("the portal url is not configured")
        if not push_key:
            raise PortalError("the worker push key is not configured; it is an environment "
                              "secret and is never sent in a message")
        self.base = base_url.rstrip("/")
        self._key = push_key
        self.worker = worker
        # Recorded on everything, because a row that cannot say where it ran is how the rule
        # against calling a runner somebody's desktop gets broken.
        self.location = location
        self.vault = vault
        if vault is not None:
            vault.register(push_key)
        # Injectable so the tests drive the whole loop with no network at all.
        self._opener = opener or self._http

    # ---- the wire ----------------------------------------------------------------
    def _http(self, action, body, query):
        q = dict(query or {})
        q["action"] = action
        q["cb"] = "%08x" % random.getrandbits(32)      # the cache in front of api.php
        url = self.base + "?" + urllib.parse.urlencode(q)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("User-Agent", UA)
        req.add_header("X-BL-PUSH", self._key)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw or "{}")
            except ValueError:
                return {"ok": False, "error": "http %d" % e.code}
        except Exception as e:
            # A NETWORK FAILURE IS NOT A REFUSAL AND NOT A SUCCESS. It raises, so a caller
            # cannot read it as "the portal said no" and carry on to the next step.
            raise PortalError("the portal could not be reached: %s" % type(e).__name__)

    def _say(self, action, body=None, query=None):
        b = dict(body or {})
        b.setdefault("worker", self.worker)
        b.setdefault("execution_location", self.location)
        return self._opener(action, b if action != "btask_next" else None, query)

    # ---- the doors, one method each ---------------------------------------------
    def next_tasks(self, limit=3, stage="queued"):
        """What there is to do. `stage` is "queued" (new work) or "approved" (a person has
        decided, and exactly one action may now be carried out). They are separate stages
        because an APPROVED task must never be picked up by the ordinary loop and run again
        from the beginning -- its safe steps are already done and its checkpoint says so."""
        r = self._opener("btask_next", None, {"limit": limit, "stage": stage})
        return list(r.get("tasks") or [])

    def claim(self, task_id):
        return self._say("btask_claim", {"task": task_id})

    def checkpoint(self, task_id, did, verified_by, url="", title="", next_safe="", why=""):
        return self._say("btask_checkpoint", {"task": task_id, "why": why, "checkpoint": {
            "did": did, "verified_by": verified_by, "url": url, "title": title,
            "next_safe": next_safe}})

    def pend(self, task_id, action, action_class, payload, shown=None):
        return self._say("btask_pend", {"task": task_id, "action_name": action,
                                        "action_class": action_class, "payload": payload,
                                        "shown": shown or {}})

    def claim_exec(self, task_id):
        return self._say("btask_claim_exec", {"task": task_id})

    def result(self, task_id, exec_token, outcome, detail="", evidence=()):
        return self._say("btask_result", {"task": task_id, "exec_token": exec_token,
                                          "outcome": outcome, "detail": detail,
                                          "evidence": list(evidence)})

    def state(self, task_id, state, why="", portal=None, workflow=None):
        b = {"task": task_id, "state": state, "why": why}
        if portal is not None:
            b["portal"] = portal
        if workflow is not None:
            b["workflow"] = workflow
        return self._say("btask_state", b)

    def file(self, task_id, provenance):
        return self._say("btask_file", {"task": task_id, "provenance": provenance})


def from_env(env=None, location="background_ci"):
    """The worker as the runner configures it. Nothing here has a default URL or key: a
    guessed portal address is a guessed portal."""
    e = env if env is not None else os.environ
    return Portal(e.get("BL_PORTAL_URL", ""), e.get("PUSH_KEY", ""),
                  e.get("BL_WORKER_NAME", "ci-browser-worker"), location)
