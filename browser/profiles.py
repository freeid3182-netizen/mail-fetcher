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

# Read-only classes are safe to retry and to run in parallel. Everything else changes something
# outside this business's own records and needs a person. The split is the contract's §3D list,
# compressed to the two things the code must actually decide: may it be retried, and does it
# need an approval bound to its exact content.
READ_CLASSES = ("navigate", "inspect", "search", "download")
CONSEQUENTIAL_CLASSES = ("submit", "upload_final", "pay", "approve", "send", "delete", "modify")
ALL_CLASSES = READ_CLASSES + CONSEQUENTIAL_CLASSES

_HOST = re.compile(r"^[a-z0-9.-]+$")


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
        workflows[str(wname)] = {
            "class": cls,
            "approval_required": needs_approval,
            "retryable": cls in READ_CLASSES,
            "steps": steps,
            "expect": dict(w.get("expect") or {}),
        }

    return {
        "slug": slug,
        "name": name,
        "hosts": hosts,
        "login": dict(raw.get("login") or {}),
        "credential_keys": cred_keys,
        "workflows": workflows,
        "download": dict(raw.get("download") or {}),
        "notes": str(raw.get("notes") or ""),
    }


def host_allowed(profile, url):
    """Is this url inside the profile's declared hosts?

    Suffix matching on a dot boundary, so `.supplier.test` covers `a.supplier.test` but a
    host merely ENDING in those characters -- `evilsupplier.test` -- does not. That trick is
    how allow-lists are usually got around.
    """
    m = re.match(r"^[a-z]+://([^/@]*@)?([^/:?#]+)", str(url or "").strip(), re.I)
    if not m:
        return False
    host = m.group(2).lower()
    for h in profile.get("hosts", []):
        if host == h or host.endswith("." + h.lstrip(".")):
            return True
    return False


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
