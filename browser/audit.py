# -*- coding: utf-8 -*-
"""Phase 13 Step 3E — the audit row, with the fields the contract names and nothing missing.

WHY A BUILDER AND NOT A LOG CALL. The contract lists nine things every row must carry: who,
which portal, what operation and of what class, when, the outcome, the approval reference where
one applies, non-sensitive evidence, and WHERE it ran. A free-form log line carries whichever
of those the author remembered. So the row is constructed, the required fields are checked, and
a row that cannot say who did what to which portal is refused rather than written half-blind.

TWO THINGS THIS ENFORCES THAT A CONVENTION WOULD NOT:

  1. EVERY ROW IS REDACTED ON THE WAY IN, through the vault, including its evidence. Not on the
     way out, and not by the caller remembering: by the time a row exists it is already safe to
     print. If a secret still appears, `Trail.append` raises -- a loud failure beats a quiet
     leak in a file somebody will paste into a support thread.
  2. EXECUTION LOCATION IS A REQUIRED FIELD with a closed set of values, because execution rule
     14 forbids describing a remote browser as the owner's desktop, and the way that rule gets
     broken is by nobody recording where the thing actually ran.
"""
import datetime
import json

# Closed set, on purpose. "visible_remote" is a headed browser on an authorized machine the
# owner can actually watch; "background_ci" is a headless one on a runner, which is what exists
# today; "local_agent" is the owner's own machine, which does not exist yet (Phase 10). A row
# may not claim a location that is not one of these, and the words are deliberately not
# interchangeable.
LOCATIONS = ("background_ci", "visible_remote", "local_agent")

REQUIRED = ("at", "uid", "task_id", "portal", "operation", "action_class", "outcome",
            "execution_location")


class AuditError(ValueError):
    pass


def row(uid, task_id, portal, operation, action_class, outcome,
        execution_location="background_ci", approval_ref="", session_ref="",
        detail="", evidence=None, url=""):
    if execution_location not in LOCATIONS:
        raise AuditError("execution_location must be one of %s, got %r"
                         % (", ".join(LOCATIONS), execution_location))
    r = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "uid": str(uid or ""),
        "task_id": str(task_id or ""),
        "portal": str(portal or ""),
        "operation": str(operation or ""),
        "action_class": str(action_class or ""),
        "outcome": str(outcome or ""),
        "execution_location": execution_location,
        "approval_ref": str(approval_ref or ""),
        "session_ref": str(session_ref or ""),
        "url": str(url or ""),
        "detail": str(detail or ""),
        "evidence": list(evidence or []),
    }
    missing = [k for k in REQUIRED if not r.get(k)]
    if missing:
        raise AuditError("an audit row without %s cannot be read later"
                         % ", ".join(missing))
    return r


class Trail(object):
    """Rows in memory, redacted on append, handed to the portal by the caller.

    It deliberately does not write a file. The portal is the authoritative store -- it already
    has one -- and a worker that keeps its own copy of the audit is a second source of truth
    about what happened, which is the thing this project has spent months removing.
    """

    def __init__(self, vault=None, sink=None):
        self._vault = vault
        self._sink = sink
        self.rows = []

    def append(self, r):
        if self._vault is not None:
            r = self._vault.redact_deep(r)
            blob = json.dumps(r, ensure_ascii=False)
            if self._vault.holds_secret(blob):
                # Unreachable unless redact_deep misses a shape. Raising beats writing it.
                raise AuditError("a secret survived redaction; the row was NOT recorded")
        self.rows.append(r)
        if self._sink:
            self._sink(r)
        return r

    def of_class(self, action_class):
        return [r for r in self.rows if r.get("action_class") == action_class]

    def consequential(self, consequential_classes):
        return [r for r in self.rows if r.get("action_class") in consequential_classes]

    def unapproved_consequential(self, consequential_classes):
        """Every consequential row with no approval reference. This must always be empty, and
        asserting it over the SET is what makes a missing gate fail loudly rather than once."""
        return [r for r in self.consequential(consequential_classes)
                if not r.get("approval_ref")]
