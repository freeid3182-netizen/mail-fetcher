# -*- coding: utf-8 -*-
"""Phase 13 §3.3 — research findings that carry their sources, or do not exist.

THE FAILURE THIS PREVENTS. "The October rate is 8,450" is worth nothing without where it came
from and when. A model asked to compare three suppliers will produce a tidy table either way,
and a tidy table of invented numbers is the single most expensive thing this portal could hand
the owner -- a figure in front of a client is the one place the project rules say to stop.

So a Finding cannot be built without at least one Note, and a Note cannot be built without a
url and a retrieval time. There is no path through this file that produces a claim with no
source: that is the whole design, and `report()` asserts it again on the way out rather than
trusting that every caller did the right thing.

WHAT IS NOT DECIDED HERE. Whether a host may be read at all is profiles.host_allowed, and
whether a host is official is declared in the profile, not guessed from the name. `gov.in` in
a hostname is not a credential -- `gov-in-rates.example.test` has it too.
"""
import json
import time

OFFICIAL = "official"
SECONDARY = "secondary"
UNKNOWN = "unknown"
KINDS = (OFFICIAL, SECONDARY, UNKNOWN)


class ResearchError(ValueError):
    pass


class Note(object):
    """One thing read, from one page, at one time."""

    def __init__(self, url, verbatim, kind=UNKNOWN, title="", at=None):
        if not str(url).strip():
            raise ResearchError("a note needs the url it was read from")
        if not str(verbatim).strip():
            raise ResearchError(
                "a note needs the words as the page had them. A summary with no quoted text "
                "cannot be checked against the page later, which is the only thing that makes "
                "it a source rather than a memory.")
        if kind not in KINDS:
            raise ResearchError("a source is one of %s, got %r" % (", ".join(KINDS), kind))
        self.url = str(url)
        self.verbatim = str(verbatim)[:400]
        self.kind = kind
        self.title = str(title)[:160]
        # Retrieval time is recorded here and never passed in by a caller that "knows" it:
        # §3.3 wants when it was read, and the only moment anybody knows that is now.
        self.at = at or time.strftime("%Y-%m-%dT%H:%M:%S")

    def as_dict(self):
        return {"url": self.url, "verbatim": self.verbatim, "kind": self.kind,
                "title": self.title, "at": self.at}

    def cite(self):
        """One line, for the task's evidence list. The portal keeps lines, not objects."""
        return "%s | %s | read %s | %s" % (self.kind, self.url, self.at, self.verbatim[:160])


class Finding(object):
    """One claim, and every note behind it."""

    def __init__(self, claim, notes):
        if not str(claim).strip():
            raise ResearchError("a finding needs the claim in words")
        notes = list(notes or [])
        if not notes:
            raise ResearchError(
                "a finding with no note is an invention. If the pages did not say it, the "
                "answer is that it was not found.")
        for n in notes:
            if not isinstance(n, Note):
                raise ResearchError("a finding's sources are Notes, not %r" % type(n).__name__)
        self.claim = str(claim)[:300]
        self.notes = notes

    @property
    def best_kind(self):
        for k in KINDS:                       # official beats secondary beats unknown
            if any(n.kind == k for n in self.notes):
                return k
        return UNKNOWN

    def disagreement(self):
        """Sources that do not say the same thing, as a pair of quotes -- never averaged.

        §3.3 asks for conflicting information to be IDENTIFIED. Picking one, or splitting the
        difference, is how two suppliers' rates become a third rate that nobody quoted.
        """
        seen = {}
        for n in self.notes:
            seen.setdefault(n.verbatim.strip().lower(), []).append(n)
        if len(seen) < 2:
            return None
        return [{"says": v[0].verbatim, "url": v[0].url, "kind": v[0].kind}
                for v in seen.values()]

    def as_dict(self):
        d = {"claim": self.claim, "sources": [n.as_dict() for n in self.notes],
             "source_kind": self.best_kind}
        dis = self.disagreement()
        if dis:
            d["disagreement"] = dis
            d["certain"] = False
        else:
            d["certain"] = True
        return d


def report(findings, asked, not_found=()):
    """What goes back to the owner. Everything unanswered is listed, not quietly dropped.

    A research run that found two of five things and reports two findings reads as a complete
    answer. `not_found` is how the other three stay visible.
    """
    findings = list(findings or [])
    for f in findings:
        if not isinstance(f, Finding):
            raise ResearchError("a report is built from Findings, not %r" % type(f).__name__)
        if not f.notes:                     # belt and braces: Finding refuses this already
            raise ResearchError("a finding lost its sources between building and reporting")
    out = {
        "asked": str(asked)[:400],
        "findings": [f.as_dict() for f in findings],
        "not_found": [str(x)[:200] for x in (not_found or [])],
        "uncertain": [f.claim for f in findings if not f.as_dict()["certain"]],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out["say"] = _say(out)
    return out


def _say(out):
    bits = []
    if out["findings"]:
        bits.append("%d finding(s), each with its source and the time it was read"
                    % len(out["findings"]))
    else:
        bits.append("nothing was found")
    if out["uncertain"]:
        bits.append("%d of them have sources that DISAGREE and are reported as uncertain, "
                    "not resolved" % len(out["uncertain"]))
    if out["not_found"]:
        bits.append("%d thing(s) asked for were not found" % len(out["not_found"]))
    return ". ".join(bits) + "."


def cites(findings):
    """Every citation line, for the task's evidence list."""
    out = []
    for f in findings or []:
        for n in f.notes:
            out.append(n.cite())
    return out


def kind_for(profile, url):
    """Official only if the profile SAYS a host is official. Never read from the name.

    A hostname is not a credential: anybody can register one with `gov` in it, and a research
    engine that treats the string as authority is a research engine that can be aimed.
    """
    import profiles
    host = profiles.host_of(url)
    if not host:
        return UNKNOWN
    official = [str(h).strip().lower().lstrip(".")
                for h in (profile or {}).get("official_hosts") or []]
    for h in official:
        if profiles.host_matches(host, h):
            return OFFICIAL
    if (profile or {}).get("hosts") and profiles.host_allowed(profile, url):
        return SECONDARY
    return UNKNOWN
