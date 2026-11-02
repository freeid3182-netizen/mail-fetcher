# -*- coding: utf-8 -*-
"""Phase 13 Steps 3D and 9D — an approval bound to one action and one exact content.

THE SHAPE IS TAKEN FROM THE PORTAL, NOT INVENTED. m7s_gateway already does this for mail: a
person approves a specific draft, and the portal carries it out. This is the same thing for a
browser action, and the portal stays the authority -- the worker only ever CHECKS what the
portal hands it.

THE PROPERTY, and it is one sentence: an approval cannot be made to authorise anything other
than the exact thing that was shown to the person.

That is why the binding is a HASH OF THE CONTENT rather than a flag. A flag has to be
invalidated by someone remembering to invalidate it; a hash simply stops matching. So:

  * material change -> the hash differs -> the approval is dead. Nobody has to notice.
  * a different action with the same content -> refused, the action name is in the hash.
  * expiry -> refused, with how long ago.
  * already used -> refused. An idempotency key is spent once, so a retry, a worker restart or
    two runners picking up the same job cannot file the same thing twice. This project's own
    rule: "do not resume a consequential action merely because it was the next step before a
    restart; revalidate its approval and content first."

WHAT IS NOT HERE. No approving. Nothing in this file can create an approval, and no browser
page, mail or model output is an input to one. A person approves, in the portal. §15 of the
project state: the AI approves nothing.
"""
import datetime
import hashlib
import json

DEFAULT_TTL_SECONDS = 3600


def canonical(payload):
    """One payload, one string, every time.

    sort_keys because a dict's order is not part of its meaning, and separators without spaces
    because a hash that changes when a formatter changes is a hash that will one day invalidate
    every pending approval for no reason.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def content_hash(action, payload):
    """The action name is INSIDE the hash. An approval for 'download the challan' must not be
    reusable for 'submit the challan' even if every other field is identical."""
    return hashlib.sha256(
        (str(action) + "\n" + canonical(payload)).encode("utf-8")).hexdigest()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(ts):
    try:
        d = datetime.datetime.fromisoformat(str(ts))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def pending(action, action_class, payload, portal, uid, task_id, ttl=DEFAULT_TTL_SECONDS):
    """What the portal shows the person, and what it stores while waiting.

    `shown` is the payload itself: whatever is displayed must be exactly what is hashed, or the
    person is approving one thing and the worker is carrying out another.
    """
    now = _now()
    return {
        "state": "WAITING_FOR_APPROVAL",
        "action": str(action),
        "action_class": str(action_class),
        "portal": str(portal),
        "uid": str(uid),
        "task_id": str(task_id),
        "content_hash": content_hash(action, payload),
        "shown": payload,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + datetime.timedelta(seconds=int(ttl))).isoformat(timespec="seconds"),
        "idempotency_key": hashlib.sha256(
            ("%s|%s|%s" % (task_id, action, content_hash(action, payload))).encode()
        ).hexdigest()[:32],
        "approval": None,
    }


def approve(p, by_uid, decided_at=None):
    """Record a PERSON's decision. Called by the portal, never by the model or the worker."""
    p = dict(p)
    p["approval"] = {
        "by": str(by_uid),
        "at": (decided_at or _now().isoformat(timespec="seconds")),
        "approved_hash": p["content_hash"],
    }
    p["state"] = "APPROVED"
    return p


def reject(p, by_uid, why=""):
    p = dict(p)
    p["approval"] = {"by": str(by_uid), "at": _now().isoformat(timespec="seconds"),
                     "rejected": True, "why": str(why)}
    p["state"] = "REJECTED"
    return p


def may_execute(p, action, payload_now, spent_keys=()):
    """May this be carried out, right now, with THIS content? (ok, why)

    Checked in the order that fails safest: is it approved at all, is it still valid, is it the
    same action, is it the same content, has it already been done. Every refusal says which,
    because "approval invalid" is useless to whoever has to fix it.
    """
    if not isinstance(p, dict):
        return False, "there is no pending action"
    if p.get("state") == "REJECTED":
        return False, "it was rejected by %s" % (p.get("approval") or {}).get("by", "someone")
    if p.get("state") != "APPROVED" or not (p.get("approval") or {}).get("approved_hash"):
        return False, "it is %s and has no approval" % p.get("state", "unknown")
    if (p.get("approval") or {}).get("rejected"):
        return False, "the recorded decision is a rejection"

    exp = _parse(p.get("expires_at"))
    if exp is None:
        return False, "the pending action has no readable expiry"
    if _now() > exp:
        return False, "the approval expired at %s" % p["expires_at"]

    if str(action) != str(p.get("action")):
        return False, ("the approval is for %r and this is %r"
                       % (p.get("action"), action))

    now_hash = content_hash(action, payload_now)
    if now_hash != p.get("content_hash"):
        return False, ("the content changed since it was approved "
                       "(approved %s…, now %s…)" % (str(p.get("content_hash"))[:12],
                                                    now_hash[:12]))
    if now_hash != (p.get("approval") or {}).get("approved_hash"):
        return False, "the approval was recorded against different content"

    if p.get("idempotency_key") in set(spent_keys or ()):
        return False, ("it has already been carried out (idempotency key %s)"
                       % p.get("idempotency_key"))
    return True, "approved by %s for this exact content" % (p["approval"]["by"])
