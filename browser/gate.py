# -*- coding: utf-8 -*-
"""Phase 13 Step 3D — the one place that answers "may this run", and refuses by default.

THE EXIT CONDITION THIS SERVES, in the contract's words: "Unauthorized or unapproved
consequential actions are blocked by actual backend enforcement, not merely by prompt
instructions." A rule in a prompt is a request; this is a function that returns False.

It composes what already exists rather than restating it:

  * profiles.allowed  -- is this workflow within what the PERSON was granted (an intersection,
    so a profile cannot grant what the login does not have)
  * vault.have        -- are the credentials this portal needs actually present, checked BEFORE
    a browser is launched
  * approval.may_execute -- for a consequential class, is there a person's approval bound to
    this exact action and this exact content, unexpired and unspent

REFUSAL IS THE DEFAULT AND IT IS STRUCTURAL. `decide` returns a tuple whose first element is
False unless every check passed; there is no path through it that returns True by falling off
the end, and an unknown action class is refused rather than treated as a read. That matters
more than it looks: the usual way a gate like this fails is a new class nobody classified,
arriving as neither read nor consequential and sailing through.
"""
import approval
import profiles


def decide(profile, workflow, grant, vault=None,
           pending=None, payload_now=None, spent_keys=()):
    """(ok, why, facts). `facts` carries what an audit row needs, so the caller cannot
    accidentally record a different class than the one that was checked."""
    facts = {"portal": profile.get("slug", ""), "workflow": workflow, "action_class": "",
             "needs_approval": False, "approval_ref": "", "credentials": []}

    ok, cls, _profile_says, why = profiles.allowed(profile, workflow, grant)
    facts["action_class"] = cls

    # WHETHER AN APPROVAL IS NEEDED IS DERIVED FROM THE CLASS, NOT READ FROM THE PROFILE.
    # profiles.load() already refuses a profile that sets approval_required contrary to its
    # class -- but nothing obliges a caller to have gone through load(), and a dict that skipped
    # it, or was edited after it, would arrive here saying "no approval needed" and be believed.
    # One line, and the fail-open shape stops existing. Flagged by the commit security review.
    needs_approval = cls in profiles.CONSEQUENTIAL_CLASSES
    facts["needs_approval"] = bool(needs_approval)
    if not ok:
        return False, why, facts

    # An unknown class is refused. Reached only if the class lists and the profile loader ever
    # drift apart, which is exactly when a silent default would be worst.
    known = set(profiles.READ_CLASSES) | set(profiles.CONSEQUENTIAL_CLASSES)
    if cls not in known:
        return False, "action class %r is not classified; refusing by default" % cls, facts

    if vault is not None:
        have = vault.have(profile.get("credential_keys") or [])
        facts["credentials"] = have["needed"]
        if not have["ok"]:
            return (False,
                    "the credentials this portal needs are not configured: %s. They are set as "
                    "worker secrets by their KEY NAME and never sent in a message."
                    % ", ".join(have["missing"]), facts)

    if not needs_approval:
        return True, "a %s action, granted, no approval needed" % cls, facts

    may, awhy = approval.may_execute(pending or {}, workflow,
                                         payload_now if payload_now is not None
                                         else (pending or {}).get("shown"),
                                         spent_keys)
    if not may:
        return False, "a %s action needs a person's approval: %s" % (cls, awhy), facts
    facts["approval_ref"] = (pending or {}).get("idempotency_key", "")
    return True, awhy, facts
