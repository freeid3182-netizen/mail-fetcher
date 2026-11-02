# -*- coding: utf-8 -*-
"""Phase 13 Steps 3 and 4 — targeted tests for the guards: vault, approval, gate, audit, files.

No browser, no network, no portal, no credential. Every input here is made up on the spot.

WHAT THIS SUITE IS FOR. Steps 3 and 4 add five small modules whose whole job is to REFUSE. Code
that refuses is the easiest kind to break without noticing, because the happy path keeps
working: a gate that stopped checking looks exactly like a gate that is satisfied. So each
property is asserted over its SET, per the project's rule 5 -- every consequential class, every
invalidating condition, every required audit field, every redactable shape. A new class, a new
field or a new condition added without a thought fails here rather than slipping past.

The four headline properties, in one line each:

  1. No consequential action runs without a PERSON's approval bound to its exact content.
  2. An approval cannot be stretched to cover a different action, different content, a later
     time, or a second run.
  3. A secret cannot reach a result, an audit row or a screenshot.
  4. A downloaded file is not a document until four separate questions have been asked of it.

Usage:  .venv-browser/Scripts/python mail-fetcher/browser/test_guards.py
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import approval      # noqa: E402
import audit         # noqa: E402
import files         # noqa: E402
import gate          # noqa: E402
import profiles      # noqa: E402
import vault         # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if not detail else "%s  [%s]" % (name, detail))


PROFILE = profiles.load({
    "slug": "fictional-supplier",
    "name": "Fictional Supplier Portal",
    "hosts": ["supplier.test"],
    "credential_keys": ["SUPPLIER_USER", "SUPPLIER_PASS"],
    "workflows": dict(
        (c, {"class": c, "steps": []}) for c in profiles.ALL_CLASSES),
})
ENV = {"BROWSER_CRED_SUPPLIER_USER": "atoz-bot",
       "BROWSER_CRED_SUPPLIER_PASS": "s3cret-not-real-abcdef"}
ALL = list(profiles.ALL_CLASSES)


# ==================================================================== VAULT
V = vault.Vault(ENV)

# A key NAME, never a value. Asserted over a set of the ways a value gets passed by mistake.
bad = ["s3cret-not-real-abcdef", "supplier_pass", "", None, "AB", "A" * 60,
       "PASS WORD", "PASS-WORD", "pass\nWORD"]
refused = []
for b in bad:
    try:
        V.resolve(b)
    except ValueError:
        refused.append(b)
ok("vault/EVERY non-key-name is refused, so a value cannot be passed as a key",
   len(refused) == len(bad), "accepted: %r" % [b for b in bad if b not in refused])

ok("vault/a key name resolves", V.resolve("SUPPLIER_PASS") == ENV["BROWSER_CRED_SUPPLIER_PASS"])
ok("vault/a key with no value is None, not an error", V.resolve("MISSING_KEY_HERE") is None)
ok("vault/the audit record is KEY NAMES only",
   V.used_keys() == ["SUPPLIER_PASS"] and not V.holds_secret(json.dumps(V.used_keys())),
   str(V.used_keys()))

# Resolving registered the value for redaction. Over every shape a result or a row can be.
SEC = ENV["BROWSER_CRED_SUPPLIER_PASS"]
shapes = {
    "a string": SEC,
    "in a sentence": "login failed for %s at the portal" % SEC,
    "a dict value": {"detail": SEC},
    "a nested dict": {"evidence": [{"field": "password", "value": SEC}]},
    "a list": [SEC, "fine"],
    "a tuple": ("a", SEC),
    "a url": "https://supplier.test/login?pw=" + SEC,
    "an audit row": {"detail": SEC, "evidence": [SEC], "url": SEC},
}
leaked = [k for k, s in shapes.items() if V.holds_secret(json.dumps(V.redact_deep(s)))]
ok("vault/the secret is gone from EVERY shape a result or a row can take",
   not leaked, "still leaking: %s" % leaked)

# A secret containing another must not be half-replaced into something readable.
V2 = vault.Vault({})
V2.register("abcd1234"); V2.register("abcd1234efgh")
ok("vault/the longest secret is replaced first",
   V2.redact("x abcd1234efgh y") == "x [REDACTED] y", V2.redact("x abcd1234efgh y"))
ok("vault/something too short to be a secret is not registered",
   vault.Vault({}).register("ab").count() == 0)

ok("vault/have() answers before a browser is launched",
   V.have(["SUPPLIER_USER", "SUPPLIER_PASS"])["ok"] is True
   and V.have(["SUPPLIER_USER", "NOT_SET_AT_ALL"]) == {
       "needed": ["SUPPLIER_USER", "NOT_SET_AT_ALL"],
       "missing": ["NOT_SET_AT_ALL"], "ok": False})
ok("vault/resolve_quietly does not register, so a presence check cannot leak",
   vault.Vault(ENV).resolve_quietly("SUPPLIER_PASS") is True
   and vault.Vault(ENV).count() == 0)

# The masking list must actually cover the three field kinds the contract names.
sels = " ".join(vault.SENSITIVE_SELECTORS).lower()
ok("vault/password, OTP and captcha fields are all in the masking set",
   "password" in sels and "otp" in sels and "one-time-code" in sels and "captcha" in sels,
   sels)


# ================================================================= APPROVAL
PAY = {"portal": "fictional-supplier", "doc": "PO-6204019486", "amount": "150000.00"}
p0 = approval.pending("send_ack", "submit", PAY, "fictional-supplier", "u1", "task-1")
ok("approval/a new one waits and cannot execute",
   p0["state"] == "WAITING_FOR_APPROVAL" and approval.may_execute(p0, "send_ack", PAY)[0] is False)

appr = approval.approve(p0, "owner")
ok("approval/once a PERSON approves, that exact content may run",
   approval.may_execute(appr, "send_ack", PAY) == (
       True, "approved by owner for this exact content"),
   str(approval.may_execute(appr, "send_ack", PAY)))

# Every way an approval can be invalid. One loop, so a new condition that nobody wires up fails.
expired = dict(appr, expires_at=(datetime.datetime.now(datetime.timezone.utc)
                                 - datetime.timedelta(seconds=5)).isoformat(timespec="seconds"))
conditions = {
    "nothing pending at all": ({}, "send_ack", PAY, ()),
    "not a dict": (None, "send_ack", PAY, ()),
    "still waiting": (p0, "send_ack", PAY, ()),
    "rejected": (approval.reject(p0, "owner", "no"), "send_ack", PAY, ()),
    "expired": (expired, "send_ack", PAY, ()),
    "no readable expiry": (dict(appr, expires_at="not a date"), "send_ack", PAY, ()),
    "a DIFFERENT action, same content": (appr, "pay_now", PAY, ()),
    "the amount changed": (appr, "send_ack", dict(PAY, amount="1500000.00"), ()),
    "a field was added": (appr, "send_ack", dict(PAY, note="urgent"), ()),
    "a field was removed": (appr, "send_ack", {"portal": PAY["portal"], "doc": PAY["doc"]}, ()),
    "approved against other content": (
        dict(appr, approval=dict(appr["approval"], approved_hash="deadbeef")), "send_ack",
        PAY, ()),
    "already carried out": (appr, "send_ack", PAY, (appr["idempotency_key"],)),
}
allowed = [k for k, a in conditions.items() if approval.may_execute(*a)[0]]
ok("approval/EVERY invalidating condition refuses, and each says which",
   not allowed and all(approval.may_execute(*a)[1] for a in conditions.values()),
   "wrongly allowed: %s" % allowed)

ok("approval/the action name is inside the hash",
   approval.content_hash("download", PAY) != approval.content_hash("submit", PAY))
ok("approval/key order is not part of the content",
   approval.content_hash("x", {"a": 1, "b": 2}) == approval.content_hash("x", {"b": 2, "a": 1}))
ok("approval/what was SHOWN is what was hashed",
   approval.content_hash(p0["action"], p0["shown"]) == p0["content_hash"])
ok("approval/the idempotency key is bound to the task, action and content",
   approval.pending("send_ack", "submit", PAY, "s", "u1", "task-2")["idempotency_key"]
   != p0["idempotency_key"])
ok("approval/nothing in the module can approve on its own",
   "AI" not in dir(approval) and not any(
       n for n in dir(approval) if n.startswith("auto")), str(dir(approval)))


# ===================================================================== GATE
GRANT = list(ALL)

# The headline property, over the whole consequential set: no approval, no action.
ran = [c for c in profiles.CONSEQUENTIAL_CLASSES
       if gate.decide(PROFILE, c, GRANT, vault=V)[0]]
ok("gate/EVERY consequential class is refused with no approval at all",
   not ran, "ran anyway: %s" % ran)

ran = [c for c in profiles.CONSEQUENTIAL_CLASSES
       if gate.decide(PROFILE, c, GRANT, vault=V,
                      pending=approval.pending(c, c, PAY, "s", "u1", "t"),
                      payload_now=PAY)[0]]
ok("gate/and refused with an approval that is only PENDING",
   not ran, "ran anyway: %s" % ran)

ran, refusals = [], []
for c in profiles.CONSEQUENTIAL_CLASSES:
    pend = approval.approve(approval.pending(c, c, PAY, "s", "u1", "t"), "owner")
    okk, why, facts = gate.decide(PROFILE, c, GRANT, vault=V, pending=pend, payload_now=PAY)
    (ran if okk else refusals).append(c)
    if okk and facts.get("approval_ref") != pend["idempotency_key"]:
        refusals.append(c + "/no ref")
ok("gate/with a person's approval, every consequential class runs and carries its ref",
   sorted(ran) == sorted(profiles.CONSEQUENTIAL_CLASSES) and not refusals,
   "refused: %s" % refusals)

ran = [c for c in profiles.CONSEQUENTIAL_CLASSES
       if gate.decide(PROFILE, c, GRANT, vault=V,
                      pending=approval.approve(
                          approval.pending(c, c, PAY, "s", "u1", "t"), "owner"),
                      payload_now=PAY,
                      spent_keys=(approval.pending(c, c, PAY, "s", "u1", "t")
                                  ["idempotency_key"],))[0]]
ok("gate/a spent idempotency key stops a second run of EVERY consequential class",
   not ran, "ran twice: %s" % ran)

blocked = [c for c in profiles.READ_CLASSES
           if not gate.decide(PROFILE, c, GRANT, vault=V)[0]]
ok("gate/EVERY read class runs without an approval",
   not blocked, "blocked: %s" % blocked)

# The fail-open shape: a profile dict that never went through profiles.load(), or was edited
# after it, claiming no approval is needed. load() refuses that, but nothing obliges a caller to
# use load(), so the gate must not believe the field. Over the whole consequential set.
LIAR = dict(PROFILE, workflows=dict(
    (c, {"class": c, "approval_required": False, "retryable": True, "steps": []})
    for c in profiles.ALL_CLASSES))
ran = [c for c in profiles.CONSEQUENTIAL_CLASSES if gate.decide(LIAR, c, GRANT, vault=V)[0]]
ok("gate/a profile that claims it needs no approval is NOT believed, for any class",
   not ran, "believed for: %s" % ran)

# may_prepare exists so the safe steps of a consequential workflow can run before a person is
# shown anything. The danger in having two gates is that a caller uses the permissive one at
# the moment that needs the strict one, so the two properties are asserted over the whole set:
# prepare lets every consequential workflow START, and still marks every one of them as needing
# an approval, which is what makes the caller come back through decide().
prep = {c: gate.may_prepare(PROFILE, c, GRANT, vault=V) for c in ALL}
ok("gate/the safe steps of EVERY workflow may be prepared, read or consequential",
   all(v[0] for v in prep.values()),
   "refused: %s" % [c for c, v in prep.items() if not v[0]])
ok("gate/and EVERY consequential one still comes back marked as needing a person",
   all(prep[c][2]["needs_approval"] for c in profiles.CONSEQUENTIAL_CLASSES)
   and not any(prep[c][2]["needs_approval"] for c in profiles.READ_CLASSES),
   str({c: prep[c][2]["needs_approval"] for c in ALL}))
ok("gate/preparing says in words that it does NOT authorise the action",
   all("NOT authorised" in prep[c][1] for c in profiles.CONSEQUENTIAL_CLASSES),
   str([prep[c][1] for c in profiles.CONSEQUENTIAL_CLASSES])[:160])
ok("gate/and it carries no approval reference for anything",
   not any(prep[c][2]["approval_ref"] for c in ALL))
ok("gate/a workflow outside the grant is refused at the prepare stage too",
   gate.may_prepare(PROFILE, "submit", ["inspect"], vault=V)[0] is False)
ok("gate/and so is a missing credential, before a browser is launched",
   gate.may_prepare(PROFILE, "inspect", GRANT, vault=vault.Vault({}))[0] is False)

ok("gate/an unknown action class is refused, not treated as a read",
   gate.decide(dict(PROFILE, workflows={"w": {"class": "teleport", "approval_required": False,
                                              "retryable": True, "steps": []}}),
               "w", ["teleport"], vault=V)[0] is False)
ok("gate/a workflow the person was not granted is refused",
   gate.decide(PROFILE, "download", ["inspect"], vault=V)[0] is False)
ok("gate/a workflow the profile does not have is refused",
   gate.decide(PROFILE, "not_a_workflow", GRANT, vault=V)[0] is False)
okk, why, _ = gate.decide(PROFILE, "inspect", GRANT, vault=vault.Vault({}))
ok("gate/a missing credential refuses BEFORE a browser is launched, by key name",
   okk is False and "SUPPLIER_PASS" in why and SEC not in why, why)
ok("gate/the class checked is the class reported, so an audit row cannot disagree",
   all(gate.decide(PROFILE, c, GRANT, vault=V)[2]["action_class"] == c for c in ALL))


# ==================================================================== AUDIT
FULL = dict(uid="u1", task_id="t1", portal="fictional-supplier", operation="get_doc",
            action_class="download", outcome="ok")
ok("audit/a complete row is built", audit.row(**FULL)["execution_location"] == "background_ci")

dropped = []
for k in audit.REQUIRED:
    if k == "at":
        continue          # set by the builder itself, never by the caller
    args = dict(FULL)
    if k == "execution_location":
        continue          # covered by the closed-set check below
    args[k] = ""
    try:
        audit.row(**args)
        dropped.append(k)
    except audit.AuditError:
        pass
ok("audit/EVERY required field is actually required",
   not dropped, "accepted a row missing: %s" % dropped)

accepted = []
bad_locs = ["my desktop", "local", "", "BACKGROUND_CI", "the owner's laptop", None]
for loc in bad_locs:
    try:
        audit.row(execution_location=loc, **FULL)
        accepted.append(loc)
    except audit.AuditError:
        pass
ok("audit/execution location is a closed set, so a runner cannot be called a desktop",
   not accepted and sorted(audit.LOCATIONS) == sorted(
       ("background_ci", "visible_remote", "local_agent")),
   "accepted: %r" % accepted)

T = audit.Trail(vault=V)
T.append(audit.row(detail="logged in with %s" % SEC, url="https://supplier.test/?t=" + SEC,
                   evidence=[{"field": "password", "value": SEC}], **FULL))
ok("audit/a row is redacted ON THE WAY IN, evidence included",
   not V.holds_secret(json.dumps(T.rows)), json.dumps(T.rows)[:120])


class _Liar(object):
    """A redactor that misses, to prove the Trail raises instead of writing the row."""
    def redact_deep(self, o):
        return o

    def holds_secret(self, t):
        return True


raised = False
try:
    audit.Trail(vault=_Liar()).append(audit.row(detail="x", **FULL))
except audit.AuditError:
    raised = True
ok("audit/if redaction ever misses, the row is NOT written", raised)

# The property that must hold over the whole trail, for ever.
T2 = audit.Trail(vault=V)
for c in ALL:
    pend = approval.approve(approval.pending(c, c, PAY, "s", "u1", "t"), "owner")
    okk, why, facts = gate.decide(PROFILE, c, GRANT, vault=V, pending=pend, payload_now=PAY)
    if okk:
        T2.append(audit.row(uid="u1", task_id="t1", portal=facts["portal"], operation=c,
                            action_class=facts["action_class"], outcome="ok",
                            approval_ref=facts["approval_ref"]))
ok("audit/no consequential row ever lacks an approval reference",
   T2.unapproved_consequential(profiles.CONSEQUENTIAL_CLASSES) == []
   and len(T2.consequential(profiles.CONSEQUENTIAL_CLASSES))
   == len(profiles.CONSEQUENTIAL_CLASSES),
   str(T2.unapproved_consequential(profiles.CONSEQUENTIAL_CLASSES)))

sink = []
audit.Trail(vault=V, sink=sink.append).append(audit.row(**FULL))
ok("audit/the portal gets the row, and the worker keeps no second truth",
   len(sink) == 1 and not hasattr(audit.Trail(), "path"))


# ==================================================================== FILES
TMP = os.path.join(HERE, "_test_guards")
if not os.path.isdir(TMP):
    os.makedirs(TMP)


def w(name, data):
    path = os.path.join(TMP, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


PDF = b"%PDF-1.7\n" + b"x" * 400
cases = {
    "an HTML error page named .pdf": (w("err.pdf", b"<!doctype html><html>Session expired"),
                                      ".pdf", "it is an HTML page"),
    "a login redirect named .pdf": (w("login.pdf", b"  <HTML><body>Please sign in"),
                                    ".pdf", "it is an HTML page"),
    "an empty file": (w("empty.pdf", b""), ".pdf", "0 bytes"),
    "a file that is not there": (os.path.join(TMP, "nope.pdf"), ".pdf", "not there"),
    "a .pdf whose bytes are not a PDF": (w("fake.pdf", b"GIF89a" + b"x" * 400),
                                         ".pdf", "first bytes"),
    "a .xlsx that is not a zip": (w("fake.xlsx", b"nope" + b"x" * 400), ".xlsx", "first bytes"),
    "the wrong suffix": (w("real.pdf", PDF), ".xlsx", "expected .xlsx"),
}
wrong = [k for k, (path, suf, _) in cases.items()
         if files.validate(path, expect_suffix=suf)["ok"]]
ok("files/EVERY not-a-document is refused", not wrong, "accepted: %s" % wrong)
badwhy = [k for k, (path, suf, frag) in cases.items()
          if frag not in files.validate(path, expect_suffix=suf)["why"]]
ok("files/and each refusal says which question failed",
   not badwhy, "unclear: %s" % badwhy)

good = files.validate(w("challan-2026-10-ABC123.pdf", PDF), expect_suffix=".pdf",
                      expect_period="2026-10", expect_identifiers=["ABC123"])
ok("files/a real one passes, and records every question it was asked",
   good["ok"] and good["sha256"] and good["bytes"] == len(PDF)
   and set(good["checked"]) == {"exists", "size", "magic", "period", "identifiers", "sha256"},
   str(good)[:160])

ok("files/the wrong reporting period is caught",
   files.validate(good["path"], expect_period="2026-09")["ok"] is False
   and "wrong reporting period" in files.validate(good["path"],
                                                  expect_period="2026-09")["why"])
ok("files/a missing identifier is caught",
   files.validate(good["path"], expect_identifiers=["ZZZ999"])["ok"] is False)
ok("files/the period is matched however the portal wrote it",
   all(files.validate(w("c-%s.pdf" % v, PDF), expect_period="2026-10")["ok"]
       for v in ("2026-10", "202610", "2026_10")),
   "slash form is a directory separator and is not tested")

periods = {"2026-10": True, "2026-1": False, "26-10": False, "2026/10": False,
           "": False, None: False, "2026-10-01": False, "october": False}
wrongp = [p for p, want in periods.items() if files.period_ok(p) is not want]
ok("files/EVERY period shape is judged the same way", not wrongp, "wrong: %r" % wrongp)

# Duplicates: §9 says marked, never removed -- and a reissue is not a duplicate.
pv = files.provenance(good, "fictional-supplier", "t1", "u1",
                      "https://supplier.test/doc/99", portal_identifier="CH-99",
                      period="2026-10")
ok("files/provenance carries every field the contract names",
   set(pv) >= {"retrieved_at", "portal", "portal_identifier", "source_url", "task_id", "uid",
               "execution_location", "original_name", "stored_name", "bytes", "sha256",
               "period", "validated", "validation", "duplicate_of"}
   and pv["validated"] is True, str(sorted(pv)))

same = files.provenance(files.validate(w("copy.pdf", PDF)), "fictional-supplier", "t2", "u1",
                        "https://supplier.test/doc/99", portal_identifier="CH-99")
reissue = files.provenance(files.validate(w("rev.pdf", PDF + b"revised")),
                           "fictional-supplier", "t3", "u1",
                           "https://supplier.test/doc/99", portal_identifier="CH-99")
fresh = files.provenance(files.validate(w("other.pdf", PDF + b"other")), "fictional-supplier",
                         "t4", "u1", "https://supplier.test/doc/100",
                         portal_identifier="CH-100")
index = [pv]
d_same = files.find_duplicate(same, index)
d_reissue = files.find_duplicate(reissue, index)
ok("files/the same bytes are a duplicate",
   d_same and d_same["kind"] == "same_bytes", str(d_same))
ok("files/the same portal id with DIFFERENT bytes is a revision, not a duplicate",
   d_reissue and d_reissue["kind"] == "reissued" and "revision" in d_reissue["why"],
   str(d_reissue))
ok("files/a genuinely new document is neither", files.find_duplicate(fresh, index) is None)
marked = files.mark_duplicate(same, d_same)
ok("files/a duplicate is MARKED, and the file stays where it is",
   marked["duplicate_of"] == pv["stored_name"] and marked["duplicate_kind"] == "same_bytes"
   and os.path.exists(os.path.join(TMP, marked["stored_name"])),
   str(marked)[:120])
ok("files/nothing in the module deletes or overwrites",
   not any(n in dir(files) for n in ("delete", "remove", "overwrite", "replace_file")))

for n in os.listdir(TMP):
    os.remove(os.path.join(TMP, n))
os.rmdir(TMP)


# =================================================================== REPORT
print("\n".join("PASS  " + n for n in PASS))
if FAIL:
    print("\n".join("FAIL  " + n for n in FAIL))
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
