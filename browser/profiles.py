# -*- coding: utf-8 -*-
"""Phase 13 Step 2C — portal profiles: a website described as DATA, never as code.

WHY A PROFILE AND NOT AN INTEGRATION. The contract is explicit that a new authorized website
must not need a new development phase or hardcoded site-specific code. So a site is a JSON
document: its verified identity, the hosts it is allowed to reach, which workflows exist, and
what class of action each one is. Adding Wonder Cement is then a file, not a release.

THE TWO PROPERTIES THAT MAKE THIS SAFE, both enforced here rather than trusted:

  1. A PROFILE CANNOT GRANT WHAT THE PERSON DOES NOT HAVE. `allowed(profile, grant)` is an
     INTERSECTION with the caller's grant, so a profile that claims `submit` is still refused
     if the authenticated user was granted only `read`. A profile is a description of a site,
     never a permission. This mirrors the portal's own rule that the outbound allow-list ships
     empty and the owner names the hosts.
  2. A CONSEQUENTIAL ACTION ALWAYS NEEDS APPROVAL, and a profile may not say otherwise.
     `approval_required` is not read from the file for those classes -- it is computed. A
     profile that sets it false is rejected at load, loudly, because a config file is exactly
     where such a thing would be slipped past a review.

There is no GST profile and none is to be written: the owner removed GST portal automation from
Phase 13 on 2 Nov 2026.
"""
import json
import re
from urllib.parse import urlsplit

# Read-only classes are safe to retry and to run in parallel. Everything else changes something
# outside this business's own records and needs a person. The split is the contract's §3D list,
# compressed to the two things the code must actually decide: may it be retried, and does it
# need an approval bound to its exact content.
READ_CLASSES = ("navigate", "inspect", "search", "download")
CONSEQUENTIAL_CLASSES = ("submit", "upload_final", "pay", "approve", "send", "delete", "modify")
ALL_CLASSES = READ_CLASSES + CONSEQUENTIAL_CLASSES

_HOST = re.compile(r"^[a-z0-9.-]+$")

# WHAT A `steps` ENTRY MAY ASK FOR, as an allowlist rather than a list of things to refuse.
#
# `steps` run BEFORE a person has approved anything, so a verb in here must be one that a
# worker may carry out unattended. A commit security review found `upload` had been added to
# the worker's step loop: an upload is `upload_final` in this file's own class vocabulary --
# consequential -- and its path came out of the profile, so a reviewed file could have named
# any file on the runner and handed it to a portal. An allowlist rather than a denylist because
# the next verb somebody adds must be CLASSIFIED to be usable, and the failure then is a
# refused profile rather than an unapproved action.
#
# `click` and `type` are here, and that is the accepted position of this design rather than an
# oversight: a consequential workflow declares the ONE control a person's approval presses as
# its `act`, which is not in `steps` and is never run from the step loop. What is refused here
# is the verb the system itself classifies as consequential.
SAFE_STEP_VERBS = ("goto", "click", "type", "select", "download", "read", "read_pages",
                   "scroll", "screenshot", "tree", "back", "tab", "discover", "press")


class ProfileError(ValueError):
    """A profile that cannot be trusted is not loaded. There is no partial profile."""


def load(raw):
    """Validate and normalise one profile. Raises ProfileError with a reason, never guesses."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise ProfileError("not valid JSON: %s" % e)
    if not isinstance(raw, dict):
        raise ProfileError("a profile is an object")

    slug = str(raw.get("slug") or "").strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{1,38}$", slug):
        raise ProfileError("slug must be a short lowercase name, got %r" % slug)

    name = str(raw.get("name") or "").strip()
    if not name:
        raise ProfileError("a profile needs a human name, so an audit row can be read")

    # Leading "*." and "." are stripped so a host is always stored bare, and one bare host
    # means "this host and anything under it". Without this, `.vendor.test` matched
    # a.vendor.test but NOT vendor.test itself -- an allow-list with a hole at the apex, which
    # is the sort of thing that looks deliberate in a config file and is not.
    hosts = [str(h).strip().lower().lstrip("*").lstrip(".")
             for h in (raw.get("hosts") or []) if str(h).strip()]
    if not hosts:
        raise ProfileError("a profile with no hosts can reach nothing; name its hosts")
    for h in hosts:
        if not _HOST.match(h.lstrip(".")):
            raise ProfileError("not a host: %r" % h)

    # WHICH HOSTS THE OWNER CALLS AUTHORITATIVE (§3.3). Declared, never read off a hostname:
    # anybody can register a name with "gov" in it, and research that treats the string as
    # authority is research that can be aimed. A host cannot be official unless it is also a
    # host this profile may read at all -- otherwise the profile is calling authoritative
    # something it is not allowed to open.
    official = [str(h).strip().lower().lstrip("*").lstrip(".")
                for h in (raw.get("official_hosts") or []) if str(h).strip()]
    for h in official:
        if not _HOST.match(h):
            raise ProfileError("not a host: %r" % h)
        if not any(host_matches(h, a) for a in hosts):
            raise ProfileError(
                "official_hosts has %r, which this profile may not even read. Add it to "
                "hosts, or take it out of official_hosts." % h)

    # Credentials are NAMES here and values never. The worker resolves a name against its own
    # secret store; the profile says which keys a site needs, exactly as bl-conn-<slug>.php
    # does in the portal. A value in this file would be a secret in a repository.
    cred_keys = [str(k).strip() for k in (raw.get("credential_keys") or []) if str(k).strip()]
    for k in cred_keys:
        if not re.match(r"^[A-Z][A-Z0-9_]{2,48}$", k):
            raise ProfileError("a credential KEY NAME, not a value, e.g. WONDER_USER: %r" % k)
        if any(ch in k for ch in "=:/@"):
            raise ProfileError("that looks like a value, not a key name: %r" % k)

    workflows = {}
    for wname, w in (raw.get("workflows") or {}).items():
        if not isinstance(w, dict):
            raise ProfileError("workflow %r must be an object" % wname)
        cls = str(w.get("class") or "").strip().lower()
        if cls not in ALL_CLASSES:
            raise ProfileError("workflow %r has class %r; it must be one of: %s"
                               % (wname, cls, ", ".join(ALL_CLASSES)))
        needs_approval = cls in CONSEQUENTIAL_CLASSES
        if "approval_required" in w and bool(w["approval_required"]) != needs_approval:
            # Refused rather than corrected. A file that tried to turn an approval off is a
            # thing a person should see, not something to quietly fix and carry on from.
            raise ProfileError(
                "workflow %r sets approval_required=%r, but a %r action is %s. A profile may "
                "not change that." % (wname, w["approval_required"], cls,
                                      "always approval-gated" if needs_approval
                                      else "never approval-gated"))
        steps = w.get("steps") or []
        if not isinstance(steps, list):
            raise ProfileError("workflow %r steps must be a list" % wname)
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                raise ProfileError("workflow %r step %d must be an object" % (wname, i + 1))
            verb = str(s.get("do") or "").strip().lower()
            if verb not in SAFE_STEP_VERBS:
                raise ProfileError(
                    "workflow %r step %d asks for %r. A step runs BEFORE anybody has approved "
                    "anything, so it must be one of: %s. A consequential action is declared as "
                    "the workflow's \"act\", where a person decides on it."
                    % (wname, i + 1, verb, ", ".join(SAFE_STEP_VERBS)))

        # THE CONSEQUENTIAL STEP IS DECLARED SEPARATELY, AND THAT IS A STRUCTURAL GUARANTEE
        # RATHER THAN A CONVENTION. `steps` are the safe ones and the agent runs them; `act` is
        # the one thing a person approves, it is not in that list, and no code path reaches it
        # without a one-shot token from the portal.
        #
        # It is also what the control is NAMED BY. A commit security review found the worker
        # clicking a control whose name came out of the portal's JSON response -- free text
        # deciding which button a consequential action presses. The name belongs in the
        # profile, which is a reviewed file, and nowhere else.
        act = w.get("act")
        if needs_approval:
            if not isinstance(act, dict) or not str(act.get("do") or "").strip():
                raise ProfileError(
                    "workflow %r is a %r action, so it must declare the single step it would "
                    "carry out, as \"act\": {\"do\": ..., \"role\": ..., \"name\": ...}. The "
                    "control a consequential action presses is named in this file and never "
                    "taken from a response or a page." % (wname, cls))
            if not (str(act.get("name") or "").strip()
                    or str(act.get("selector") or "").strip()):
                raise ProfileError("workflow %r act must name the control it presses" % wname)
        elif act is not None:
            raise ProfileError(
                "workflow %r is a %r action and has nothing to approve, so it may not declare "
                "an act. A read workflow that wants to act is a misclassified workflow."
                % (wname, cls))
        workflows[str(wname)] = {
            "class": cls,
            # What this workflow is for, in a person's words. Optional, and flattened to one
            # line because it is read into a refusal message. agent.pick_workflow matches an
            # instruction against the workflow's NAME and this -- so a profile that describes
            # its workflows is the thing being read, rather than a list of synonyms kept in
            # the agent, which would have to be edited every time a portal is added.
            "means": re.sub(r"\s+", " ", str(w.get("means") or "")).strip()[:160],
            "approval_required": needs_approval,
            "retryable": cls in READ_CLASSES,
            "steps": steps,
            "act": dict(act) if isinstance(act, dict) else None,
            "expect": dict(w.get("expect") or {}),
        }

    return {
        "slug": slug,
        "name": name,
        "hosts": hosts,
        "official_hosts": official,
        "login": dict(raw.get("login") or {}),
        "credential_keys": cred_keys,
        "workflows": workflows,
        "download": dict(raw.get("download") or {}),
        "notes": str(raw.get("notes") or ""),
    }


def host_of(url):
    """The hostname a BROWSER would use, or "" if this is not an http(s) url.

    THERE IS ONE HOST RULE IN THIS WORKER AND IT IS HERE. The first version of this file had a
    regex, and `core.hands_goto` separately had `expect_host in landed_url` -- a substring test
    against the whole url, so https://evil.test/?x=supplier.test read as "landed on
    supplier.test". One rule written twice is how the weaker copy becomes the one that matters.

    urlsplit, not a regex, because a hand-written parser only has to differ from the browser's
    once to be a bypass: userinfo before the real host, a backslash the browser rewrites as a
    slash, a tab or newline the browser strips, a fragment or query carrying the expected name.
    Backslashes are normalised the way browsers do BEFORE parsing, and the control characters
    browsers silently drop are dropped here too, so this reads the same url the browser will.
    Anything that is not http or https -- file:, data:, javascript: -- has no host and is
    refused rather than interpreted.
    """
    s = str(url or "").strip()
    s = s.replace("\\", "/")
    s = re.sub(r"[\x00-\x20\x7F]", "", s)          # what browsers strip, stripped first
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    try:
        host = (parts.hostname or "").lower()      # hostname drops userinfo and the port
    except ValueError:
        return ""
    # After parsing, a host may still hold characters no real host has (an IDN homograph, an
    # encoded separator). A host this worker cannot write down plainly is a host it will not
    # visit.
    return host if host and _HOST.match(host) else ""


def host_matches(host, allowed_host):
    """Exact, or a subdomain on a DOT boundary. `evilsupplier.test` is not `supplier.test`."""
    host = str(host or "").lower()
    h = str(allowed_host or "").lower().lstrip("*").lstrip(".")
    if not host or not h:
        return False
    return host == h or host.endswith("." + h)


def host_allowed(profile, url):
    """Is this url inside the profile's declared hosts?"""
    host = host_of(url)
    if not host:
        return False
    return any(host_matches(host, h) for h in profile.get("hosts", []))


def allowed(profile, workflow, grant):
    """May this workflow run, given what the authenticated person was granted?

    Returns (ok, class, needs_approval, why). The INTERSECTION is the whole point: a profile
    describes a site's capabilities; the grant describes a person's. The smaller wins.
    """
    w = (profile.get("workflows") or {}).get(workflow)
    if not w:
        return False, "", False, "no workflow %r in profile %r" % (workflow, profile.get("slug"))
    g = set(str(x).strip().lower() for x in (grant or []))
    if w["class"] not in g:
        return (False, w["class"], w["approval_required"],
                "the person was granted %s, and this workflow is a %r action"
                % (sorted(g) or "nothing", w["class"]))
    return True, w["class"], w["approval_required"], "granted"
