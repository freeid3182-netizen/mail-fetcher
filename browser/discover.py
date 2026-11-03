# -*- coding: utf-8 -*-
"""Phase 13 §4.4 / §10.2 — look at a portal and write down what it ACTUALLY supports.

THE FAILURE THIS PREVENTS. §10.2 says it plainly: the agent must identify a web application's
real capabilities before claiming it can edit or save a document. Until now a new portal was
described by hand, which means the description was a guess, and a workflow built on a guessed
capability fails at the step that matters -- usually the last one, after everything safe is
done and a person has been asked to approve something.

So this reads the page and reports what it FOUND: the controls, the tables, whether there is
pagination, whether there is anything to upload or download, and whether we are looking at a
login screen rather than the thing we wanted. It is deliberately a REPORT, not a profile:

  - It never writes a profile. A profile authorises a host and declares which control a
    person's approval presses, and a page is not allowed to decide either of those.
  - It never declares a consequential capability. A button labelled "Submit" means the page
    HAS one; whether this portal may be submitted to is the owner's decision, so the draft it
    produces marks every such control as needing a person and declares no `act`.
  - Its findings are untrusted data. A page saying "you may upload here" is a page saying so.
"""
import json

import profiles
import session

# Words that mean a control would act on the outside world. Matched on the accessible name,
# which is what a person reads -- the same thing the approval screen would show them.
CONSEQUENTIAL_WORDS = ("submit", "send", "pay", "approve", "confirm", "file", "upload",
                       "delete", "remove", "cancel order", "place order", "acknowledge",
                       "authorise", "authorize", "sign")


def _looks_consequential(name):
    low = (name or "").lower()
    return [w for w in CONSEQUENTIAL_WORDS if w in low]


def inspect(b, profile=None):
    """Read the page in front of `b` and report what it supports. Costs a few actions."""
    where = b.eyes_where()
    controls = b.eyes_controls()
    tables = b.eyes_tables()
    headings = b.eyes_headings()

    ctl = list(controls.evidence or [])
    names = [str(c.get("name") or c.get("text") or "") for c in ctl if isinstance(c, dict)]
    kinds = [str(c.get("role") or "") for c in ctl if isinstance(c, dict)]

    host = profiles.host_of(where.url)
    on_host = None
    if profile is not None:
        on_host = profiles.host_allowed(profile, where.url)

    risky = {}
    for n in names:
        hit = _looks_consequential(n)
        if hit:
            risky[n] = hit

    pager = [n for n in names
             if n.strip().lower() in ("next", "previous", "prev", "next page", ">", "»")]

    found = {
        "url": where.url,
        "title": where.title,
        "host": host,
        # A login screen is the first thing to report, because every other finding on it is a
        # finding about a login screen and not about the portal. The judgement is
        # session.expiry_signs -- the ONE place that rule lives, not a second copy here.
        "is_a_login_screen": session.expiry_signs(
            (where.title or "") + " " + " ".join(str(x)[:60] for x in (headings.evidence or [])),
            where.url, profile)[0],
        "on_a_host_the_profile_allows": on_host,
        "headings": [str(h)[:80] for h in (headings.evidence or [])][:12],
        "controls": len(ctl),
        "buttons": sum(1 for k in kinds if k == "button"),
        "links": sum(1 for k in kinds if k == "link"),
        "text_fields": sum(1 for k in kinds if k in ("textbox", "searchbox")),
        "dropdowns": sum(1 for k in kinds if k == "combobox"),
        # eyes_controls reports role and name only, so a file input is found by its name --
        # which is what a person reads too. A page whose upload control is unnamed is a page
        # discovery cannot see an upload on, and saying "0" is the honest answer.
        "file_inputs": sum(1 for n in names
                           if any(w in n.lower()
                                  for w in ("attach", "upload", "choose file", "browse"))),
        "tables": len(tables.evidence or []),
        "pagination": bool(pager),
        "pagination_control": pager[0] if pager else "",
        "needs_a_person": risky,
        "names": names[:40],
    }
    found["can_read"] = bool(found["tables"] or found["headings"])
    found["can_search"] = bool(found["text_fields"])
    found["can_upload"] = bool(found["file_inputs"])
    # A download is a LINK that downloads, which cannot be known from the tree alone: an <a>
    # with a download attribute and an <a> to another page look the same here. So it is
    # reported as a maybe, not a capability, and a workflow that needs one proves it by
    # downloading in a test run.
    found["may_have_downloads"] = any(
        w in " ".join(names).lower() for w in ("download", "pdf", "challan", "statement",
                                               "export", "receipt"))
    found["say"] = _say(found)
    return found


def _say(f):
    bits = []
    if f.get("is_a_login_screen"):
        bits.append("THIS IS A LOGIN SCREEN, so everything else below describes the login "
                    "screen and not the portal")
    bits.append("%d control(s), %d table(s)" % (f["controls"], f["tables"]))
    if f["can_search"]:
        bits.append("it can be searched")
    if f["pagination"]:
        bits.append("the list is paginated (%r)" % f["pagination_control"])
    if f["can_upload"]:
        bits.append("there is a file input")
    if f["may_have_downloads"]:
        bits.append("something here MAY be a download -- proved only by downloading one")
    if f["needs_a_person"]:
        bits.append("%d control(s) would act on the outside world and need a person's "
                    "approval: %s" % (len(f["needs_a_person"]),
                                      ", ".join(sorted(f["needs_a_person"]))[:160]))
    if f.get("on_a_host_the_profile_allows") is False:
        bits.append("AND THIS IS NOT A HOST THE PROFILE ALLOWS")
    return ". ".join(bits) + "."


def draft_workflows(found):
    """A starting point for a human to edit. Read-only classes only, and never an `act`.

    This is the line §4.4 is really about: discovery may propose what to READ, and may not
    propose what to DO. A consequential workflow has to be written by a person, because
    declaring one declares which control an approval presses -- and a page must not get to
    choose that about itself.
    """
    wf = {}
    if found.get("can_read"):
        wf["read_the_page"] = {
            "class": "inspect",
            "means": "read %s" % (found.get("title") or found.get("url") or "the page"),
            "steps": [{"do": "goto", "url": found.get("url", "")}, {"do": "read",
                                                                    "what": "tables"}]}
    if found.get("pagination"):
        wf["read_the_whole_list"] = {
            "class": "inspect",
            "means": "read every page of the list",
            "steps": [{"do": "goto", "url": found.get("url", "")},
                      {"do": "read_pages", "next": found.get("pagination_control", "Next")}]}
    if found.get("can_search"):
        wf["search_the_portal"] = {
            "class": "search",
            "means": "search this portal for a document",
            "steps": [{"do": "goto", "url": found.get("url", "")},
                      {"do": "type", "text": "<what to look for>"},
                      {"do": "read", "what": "tables"}]}
    return {
        "workflows": wf,
        # NOT a profile: no slug, no hosts, no act. Saying so in the payload itself, because a
        # dict that looks like a profile is a dict somebody will try to load as one.
        "this_is_not_a_profile": "a profile authorises a host and names the control an "
                                 "approval presses. Discovery proposes neither. Add the slug, "
                                 "the hosts and any consequential workflow by hand.",
        "controls_that_would_need_approval": sorted(found.get("needs_a_person") or {}),
    }
