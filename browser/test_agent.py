# -*- coding: utf-8 -*-
"""Phase 13 Step 9A/9B/9C — the worker loop, against a fake portal and the local fixture.

NO NETWORK, NO PORTAL, NO CREDENTIAL. The Portal client takes an injectable opener, so every
call the agent makes is recorded in a list here and answered the way api.php would answer it.
That is the point: the properties being asserted are about WHAT THE WORKER SAYS AND IN WHAT
ORDER, and a real portal would prove the portal's rules (phase13_task does that) rather than
the worker's.

THE PROPERTIES, each asserted over its set where there is a set:

  1. A read workflow runs its steps, checkpoints each VERIFIED one, and completes -- without
     ever asking for an approval.
  2. A consequential workflow does every safe step, then STOPS. No hand touches the
     consequential step, and the run ends with the content in front of a person.
  3. No consequential action is carried out without a one-shot token from the portal.
  4. An action whose result cannot be confirmed is reported UNKNOWN, never FAILED.
  5. A step that fails ends the run; nothing after it is attempted.
  6. An unknown portal, an unknown workflow and a missing grant are each BLOCKED with a
     reason, never guessed past.

Usage:  .venv-browser/Scripts/python mail-fetcher/browser/test_agent.py
"""
import functools
import http.server
import json
import os
import socketserver
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent        # noqa: E402
import portal as portalmod  # noqa: E402
import profiles     # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if not detail else "%s  [%s]" % (name, detail))


# ---------------------------------------------------------------- the fixture site
FIX = os.path.join(HERE, "fixtures")
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIX)


class Quiet(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, *a):
        pass


httpd = Quiet(("127.0.0.1", 0), Handler)
PORT = httpd.server_address[1]
BASE = "http://127.0.0.1:%d" % PORT
threading.Thread(target=httpd.serve_forever, daemon=True).start()


# ---------------------------------------------------------------- the fake portal
class FakePortal(object):
    """Answers like api.php does, and REMEMBERS the order it was asked in.

    It is deliberately strict in the one place that matters: claim_exec hands out a token only
    when `approved` has been set by this test acting as the person. A fake that always said yes
    would make property 3 untestable, which is the property most worth having."""

    def __init__(self):
        self.said = []
        self.approved = False
        self.approved_payload = None
        self.token = "tok-" + "f" * 28
        self.pended = None
        self.files = []

    def __call__(self, action, body, query):
        self.said.append((action, body or {}))
        if action == "btask_next":
            return {"ok": True, "tasks": list(self.queue)}
        if action == "btask_pend":
            self.pended = body
            # What the person will be shown is what the portal stores, so it is also what the
            # resume will be handed back. Keeping it here is what makes the page-changed
            # assertion below a real comparison rather than a tautology.
            self.approved_payload = dict(body.get("payload") or {})
            return {"ok": True, "task": {"state": "WAITING_FOR_APPROVAL"}}
        if action == "btask_claim_exec":
            if not self.approved:
                return {"ok": False, "code": "NOT_APPROVED",
                        "error": "this task is WAITING_FOR_APPROVAL and no approved action "
                                 "may be carried out"}
            # The payload the PERSON approved, which is what the resume must find the page
            # still matching. `action` is the workflow's name, as api.php sends it -- and it is
            # deliberately NOT the name of any control, because the resume no longer reads a
            # control name out of this response.
            return {"ok": True, "exec_token": self.token, "action": "acknowledge_order",
                    "action_class": "submit",
                    "payload": dict(self.approved_payload or {})}
        if action == "btask_file":
            self.files.append(body.get("provenance"))
            return {"ok": True, "task": {"state": "EXECUTING"}}
        return {"ok": True, "task": {"state": "EXECUTING"}}

    queue = []

    def names(self):
        return [a for a, _ in self.said]

    def body(self, action):
        return [b for a, b in self.said if a == action]


def make(profile_json, instruction, grant, env=None):
    fp = FakePortal()
    p = portalmod.Portal(BASE, "test-push-key", "test-worker", "background_ci", opener=fp)
    a = agent.Agent(p, profiles_dir=os.path.join(HERE, "_noprofiles"),
                    env=env if env is not None else {}, task_dir=os.path.join(HERE, "_tasks"))
    a.profiles = {profile_json["slug"]: profiles.load(profile_json)}
    task = {"id": "bt-test-1", "uid": "boxadmin", "instruction": instruction,
            "portal": profile_json["slug"], "grant": grant,
            "checkpoint": {"step": 0}, "attempts": 0, "mode": "background"}
    return a, fp, task


SUPPLIER = {
    "slug": "fictional-supplier",
    "name": "Fictional Supplier Portal",
    "hosts": ["127.0.0.1"],
    "workflows": {
        "list_documents": {
            "class": "inspect",
            "means": "read the documents page and the purchase order table",
            "steps": [{"do": "goto", "url": BASE + "/index.html"},
                      {"do": "read", "what": "tables"},
                      {"do": "click", "role": "button", "name": "Search"}]},
        "download_po": {
            "class": "download",
            "means": "get the purchase order document",
            "steps": [{"do": "goto", "url": BASE + "/index.html"},
                      {"do": "download", "role": "link", "name": "Download PO",
                       "suffix": ".txt", "identifier": "6204019486",
                       "identifiers": ["6204019486"]}]},
        # The same download, declared as a .pdf. The fixture serves a .txt, so this is the
        # "an error page named challan.pdf" case arriving by the honest road -- and the worker
        # must record NO file at all rather than one it could not stand behind.
        "download_wrongtype": {
            "class": "download",
            "means": "fetch a pdf that is not one",
            "steps": [{"do": "goto", "url": BASE + "/index.html"},
                      {"do": "download", "role": "link", "name": "Download PO",
                       "suffix": ".pdf"}]},
        "acknowledge_order": {
            "class": "submit",
            "means": "acknowledge the order to the supplier",
            "steps": [{"do": "goto", "url": BASE + "/index.html"},
                      {"do": "type", "text": "6204019486", "name": "PO number"}],
            # The one control a person's approval authorises, named HERE. The fixture's
            # "Looks Important" is perfectly clickable and changes nothing, so this workflow
            # also exercises the outcome that cannot be confirmed.
            "act": {"do": "click", "role": "button", "name": "Looks Important"}},
        "break_here": {
            "class": "inspect",
            "means": "a workflow whose second step cannot succeed",
            "steps": [{"do": "goto", "url": BASE + "/index.html"},
                      {"do": "click", "role": "button", "name": "A Button That Is Not There"},
                      {"do": "read"}]},
    },
}

print("=" * 96)
print("PHASE 13 AGENT (worker)  what the worker says, in what order, and what it will not do")
print("=" * 96)

# ============================================================ 1. A READ WORKFLOW
a, fp, task = make(SUPPLIER, "list the documents on the supplier portal", ["inspect"])
line = a.run_task(task)
ok("read/a read workflow runs and completes", "COMPLETED" in line, line)
ok("read/it claimed the task before touching a browser",
   fp.names()[0] == "btask_claim", str(fp.names()[:3]))
ok("read/it checkpointed EVERY step, not just the last",
   fp.names().count("btask_checkpoint") == 3, str(fp.names()))
cps = fp.body("btask_checkpoint")
ok("read/and every checkpoint says HOW the step was verified",
   all(c["checkpoint"]["verified_by"] for c in cps),
   str([c["checkpoint"]["verified_by"][:30] for c in cps]))
ok("read/each one names the next SAFE action, so a resume does not guess",
   [c["checkpoint"]["next_safe"] for c in cps] == ["read", "click", "finish"],
   str([c["checkpoint"]["next_safe"] for c in cps]))
ok("read/NO approval was asked for, because nothing consequential was done",
   "btask_pend" not in fp.names() and "btask_claim_exec" not in fp.names(), str(fp.names()))
ok("read/every call said where it was running",
   all(b.get("execution_location") == "background_ci"
       for a_, b in fp.said if a_ != "btask_next"),
   str([b.get("execution_location") for _, b in fp.said]))

# ============================================================ 2. A DOWNLOAD, VALIDATED
a, fp, task = make(SUPPLIER, "download the purchase order document", ["download"])
line = a.run_task(task)
ok("file/a download workflow completes", "COMPLETED" in line, line)
ok("file/the file was recorded only with its hash and only once validated",
   fp.files and fp.files[0]["validated"] is True and len(fp.files[0]["sha256"]) == 64,
   json.dumps(fp.files)[:200])
ok("file/and its provenance says where it ran and where it came from",
   fp.files and fp.files[0]["execution_location"] == "background_ci"
   and "127.0.0.1" in fp.files[0]["source_url"], json.dumps(fp.files)[:200])

a2, fp2, task2 = make(SUPPLIER, "fetch a pdf that is not one wrongtype", ["download"])
line2 = a2.run_task(task2)
ok("file/a download that is not the type it was asked for records NO file",
   not fp2.files, json.dumps(fp2.files)[:160])
ok("file/and the reason is checkpointed in the words files.validate used",
   any("did not validate" in (b.get("checkpoint") or {}).get("did", "")
       and "expected .pdf" in (b.get("checkpoint") or {}).get("verified_by", "")
       for b in fp2.body("btask_checkpoint")),
   json.dumps(fp2.body("btask_checkpoint"))[:260])

# ============================================================ 3. THE CONSEQUENTIAL STOP
a, fp, task = make(SUPPLIER, "acknowledge the order", ["submit"])
line = a.run_task(task)
ok("stop/a consequential workflow STOPS and waits for a person",
   "WAITING_FOR_APPROVAL" in line, line)
ok("stop/it asked for an approval and did NOT claim an execution",
   "btask_pend" in fp.names() and "btask_claim_exec" not in fp.names(), str(fp.names()))
pay = (fp.pended or {}).get("payload") or {}
ok("stop/the content handed over says which portal, which workflow and which page",
   pay.get("portal") == "fictional-supplier" and pay.get("workflow") == "acknowledge_order"
   and "127.0.0.1" in pay.get("page_url", ""), json.dumps(pay)[:200])
ok("stop/and what the person is SHOWN is what was handed over",
   (fp.pended or {}).get("shown") == pay)
ok("stop/the safe preparation really did happen first",
   fp.names().count("btask_checkpoint") == 2, str(fp.names()))

task["workflow"] = "acknowledge_order"      # as api.php records it when the worker pends

# THE PROPERTY. Without the portal's token there is no road to the action.
line = agent.resume_approved(a, task)
ok("token/with no approval, the action is NOT carried out",
   "not carried out" in line and "btask_result" not in fp.names(), line)

# THE FIRST THING THE SECURITY REVIEW FOUND. The resume used to call eyes_where on a fresh,
# blank browser and call that revalidation. It must go BACK to the page the action was approved
# on -- and if that page is not what was approved, press nothing.
moved = dict(fp.approved_payload or {})
fp.approved_payload = dict(moved, page_url=BASE + "/other.html")
fp.approved = True
line = agent.resume_approved(a, task)
rep = fp.body("btask_result")[-1]
ok("page/if the page is not the one that was approved, NOTHING is pressed",
   "not carried out" in line and rep["outcome"] == "FAILED"
   and "NOTHING was carried out" in rep["detail"], json.dumps(rep)[:200])
ok("page/and it says the thing must be approved again, rather than retrying",
   "approved again" in rep["detail"], rep["detail"][:120])

fp.approved_payload = moved
line = agent.resume_approved(a, task)
ok("token/once a person has approved, it is carried out once and reported",
   "btask_result" in fp.names(), line)
rep = fp.body("btask_result")[-1]
ok("token/the report carries the one-shot token it was given",
   rep["exec_token"] == fp.token, str(rep)[:140])
ok("token/and the outcome is one of the closed set",
   rep["outcome"] in ("COMPLETED", "PARTIAL", "FAILED", "UNKNOWN"), rep["outcome"])
# Kept, because later assertions push more reports onto the list and this is the one that
# matters: the report from the run that actually pressed the control.
REP_PRESSED = rep
# The host is checked by going back through hands_goto with the profile's host expected.
# "localhost" and "127.0.0.1" serve the same fixture and are DIFFERENT HOSTS, so an approved
# url on the wrong one must be refused before anything is pressed.
fp.approved_payload = dict(moved, page_url="http://localhost:%d/index.html" % PORT)
fp.approved = True
line = agent.resume_approved(a, task)
rep = fp.body("btask_result")[-1]
ok("page/an approved url on a host the profile does not list is refused",
   "not carried out" in line and rep["outcome"] == "FAILED", json.dumps(rep)[:200])
fp.approved_payload = moved

# THE SECOND THING THE REVIEW FOUND. The control pressed used to be named by the portal's JSON
# response. It is named by the profile now, and this response's `action` is the workflow name,
# which is not a control on the page at all.
AGENT_SRC = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
ok("control/the control is named in the PROFILE, never in a response",
   'go.get("action")' not in AGENT_SRC and 'act.get("name")' in AGENT_SRC)

noact = dict(SUPPLIER)
noact["workflows"] = {"acknowledge_order": {
    "class": "submit", "steps": [], "means": "no act declared"}}
refused_classes = []
for cls in profiles.CONSEQUENTIAL_CLASSES:
    try:
        profiles.load(dict(SUPPLIER, workflows={"w": {"class": cls, "steps": []}}))
    except profiles.ProfileError as e:
        if "act" in str(e):
            refused_classes.append(cls)
ok("control/EVERY consequential class must declare the step it would carry out",
   sorted(refused_classes) == sorted(profiles.CONSEQUENTIAL_CLASSES),
   "loaded without one: %s" % sorted(set(profiles.CONSEQUENTIAL_CLASSES)
                                     - set(refused_classes)))
read_with_act = []
for cls in profiles.READ_CLASSES:
    try:
        profiles.load(dict(SUPPLIER, workflows={"w": {
            "class": cls, "steps": [], "act": {"do": "click", "name": "Pay"}}}))
        read_with_act.append(cls)
    except profiles.ProfileError:
        pass
ok("control/and a READ workflow may not declare one at all",
   not read_with_act, "allowed an act: %s" % read_with_act)

# ============================================================ 4. UNCONFIRMED IS UNKNOWN
# The fixture's "Looks Important" button is perfectly clickable and changes nothing, which the
# core calls a failure -- so the approved action cannot be confirmed. That must be UNKNOWN.
ok("unknown/an approved action that cannot be confirmed is UNKNOWN, never FAILED",
   REP_PRESSED["outcome"] == "UNKNOWN"
   and "could not be confirmed" in REP_PRESSED["detail"], json.dumps(REP_PRESSED)[:200])
ok("unknown/and the words do not claim it failed",
   "failed" not in REP_PRESSED["detail"].lower(), REP_PRESSED["detail"][:120])

# ============================================================ 5. A FAILED STEP ENDS THE RUN
a, fp, task = make(SUPPLIER, "break here please", ["inspect"])
line = a.run_task(task)
ok("fail/a step that did not do what it claims ends the run",
   "FAILED at step 2" in line, line)
ok("fail/nothing after it was attempted",
   fp.names().count("btask_checkpoint") == 1, str(fp.names()))
ok("fail/and the failure was reported as a state, with a reason",
   any(b.get("state") == "FAILED" and b.get("why") for _, b in fp.said
       if _ == "btask_state"), str(fp.body("btask_state"))[:160])

# ============================================================ 6. WHAT IT REFUSES TO GUESS
cases = {
    "a portal with no profile": (dict(SUPPLIER, slug="fictional-supplier"), "do something",
                                 ["inspect"], "nosuchportal"),
    "an instruction naming no workflow": (SUPPLIER, "xyzzy plugh", ["inspect"], None),
    "a workflow the person was not granted": (SUPPLIER, "acknowledge the order", ["inspect"],
                                              None),
}
blocked = {}
for label, (prof, instr, grant, override) in cases.items():
    a, fp, task = make(prof, instr, grant)
    if override:
        task["portal"] = override
    blocked[label] = a.run_task(task)
ok("refuse/EVERY one of them is BLOCKED rather than guessed past",
   all("BLOCKED" in v for v in blocked.values()), json.dumps(blocked))
ok("refuse/and each refusal says what was missing",
   "no profile" in blocked["a portal with no profile"]
   and "no workflow" in blocked["an instruction naming no workflow"]
   and "granted" in blocked["a workflow the person was not granted"],
   json.dumps(blocked))

# The one that would be worst: a profile chosen from the words of an instruction.
a, fp, task = make(SUPPLIER, "get the challan from the fictional-supplier portal", ["inspect"])
task["portal"] = ""
ok("refuse/a profile is NEVER selected from the words of an instruction",
   "BLOCKED" in a.run_task(task))

# ============================================================ 7. THE CLIENT ITSELF
def raises(f):
    try:
        f()
        return False
    except portalmod.PortalError:
        return True


ok("client/no url and no key each mean no client, rather than a guessed address",
   raises(lambda: portalmod.Portal("", "k", "w", "background_ci"))
   and raises(lambda: portalmod.Portal(BASE, "", "w", "background_ci")))
ok("client/the client sends a user agent the host's firewall will not 403",
   "AtoZ" in portalmod.UA and "urllib" not in portalmod.UA.lower(), portalmod.UA)
ok("client/and busts the cache in front of api.php on every call",
   'q["cb"]' in open(os.path.join(HERE, "portal.py"), encoding="utf-8").read())

httpd.shutdown()
print()
for f in FAIL:
    print("   FAIL  " + f)
print("=" * 96)
print("PHASE 13 WORKER AGENT: %d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
