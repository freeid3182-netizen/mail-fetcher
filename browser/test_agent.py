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

# =========== AUTHENTICATED PROFILES AND THE LOGIN THE WORKER CANNOT DO (§5.3/5.5) ===========
import shutil        # noqa: E402
import tempfile      # noqa: E402

import session       # noqa: E402

os.environ[session.KEY_ENV] = "a passphrase only the worker has"
PDIR = tempfile.mkdtemp(prefix="atoz-agent-prof-")

# 1. NO STORED SESSION IS NOT AN ERROR. The first task for a portal runs signed out, which is
#    §5.3's whole premise, and it must not look like a crash.
a1, fp1, t1 = make(SUPPLIER, "list the documents", ["inspect"])
a1.sessions = session.SessionStore(os.path.join(PDIR, "empty"))
fp1.queue = [t1]
said1 = a1.run_task(t1)
ok("sess/with no stored session the task still runs, signed out",
   "COMPLETED" in said1, said1)
ok("sess/and nothing is claimed about a connection that does not exist",
   not fp1.body("btask_conn_say"), str(fp1.body("btask_conn_say")))

# 2. A STORED SESSION THAT WORKS writes 'verified', and only with the page that proved it --
#    the portal refuses the word without one, so the worker must supply it.
a2, fp2, t2 = make(SUPPLIER, "list the documents", ["inspect"])
st2 = session.SessionStore(os.path.join(PDIR, "good"))
st2.save("fictional-supplier",
         {"cookies": [{"name": "SID", "value": "x", "domain": "127.0.0.1", "path": "/"}],
          "origins": []},
         a2.profiles["fictional-supplier"], verified_by="set up by this test")
a2.sessions = st2
a2.run_task(t2)
says2 = fp2.body("btask_conn_say")
ok("sess/a stored session that reaches a real page is reported VERIFIED",
   [s for s in says2 if s.get("status") == "verified"], str(says2)[:200])
ok("sess/and the report carries the page that proved it, never a bare claim",
   all(str(s.get("detail", "")).strip() for s in says2 if s.get("status") == "verified"),
   str(says2)[:200])
ok("sess/it is reported ONCE, not after every step",
   len([s for s in says2 if s.get("status") == "verified"]) == 1, str(len(says2)))

# 3. A SESSION FOR THE WRONG PORTAL is refused by the store, reported as expired, and the task
#    still runs: the isolation refusal must not lose the owner's task.
a3, fp3, t3 = make(SUPPLIER, "list the documents", ["inspect"])
st3 = session.SessionStore(os.path.join(PDIR, "crossed"))
OTHER = profiles.load({"slug": "elsewhere", "name": "Elsewhere",
                       "hosts": ["elsewhere.test"],
                       "workflows": {"look": {"class": "inspect", "steps": [],
                                              "means": "look"}}})
st3.save("fictional-supplier",
         {"cookies": [{"name": "SID", "value": "x", "domain": "elsewhere.test", "path": "/"}],
          "origins": []}, OTHER, verified_by="set up by this test")
a3.sessions = st3
said3 = a3.run_task(t3)
says3 = fp3.body("btask_conn_say")
ok("sess/a session belonging to another portal is NOT loaded, and is reported expired",
   [s for s in says3 if s.get("status") == "expired"], str(says3)[:200])
ok("sess/and the task is not lost over it", "COMPLETED" in said3, said3)

# 4. §5.5 — A LOGIN STEP A WORKER CANNOT DO. The fixture's other.html is a sign-in page, so a
#    workflow that lands there is the real shape of an expired session.
EXPIRED = dict(SUPPLIER)
EXPIRED["workflows"] = dict(SUPPLIER["workflows"])
EXPIRED["workflows"]["lands_on_login"] = {
    "class": "inspect", "means": "a workflow whose portal has signed us out",
    "steps": [{"do": "goto", "url": BASE + "/other.html"}, {"do": "read"}]}
a4, fp4, t4 = make(EXPIRED, "lands on login", ["inspect"])
a4.sessions = session.SessionStore(os.path.join(PDIR, "expired"))
said4 = a4.run_task(t4)
ok("auth/a sign-in page stops the task for the OWNER, not as a failure",
   "WAITING_FOR_AUTH" in said4 and "FAILED" not in said4, said4)
na = fp4.body("btask_need_auth")
ok("auth/and the portal is told which site wants what, in words the owner can act on",
   na and na[0].get("portal") == "fictional-supplier" and len(na[0].get("why", "")) > 10,
   str(na)[:200])
ok("auth/no state word is sent that would retry it from the queue",
   "FAILED" not in [b.get("state") for b in fp4.body("btask_state")],
   str([b.get("state") for b in fp4.body("btask_state")]))

# The worker never writes 'stored': that word means the owner logged in, and api.php refuses it
# from a worker. Asserted on the SOURCE, because a run that happens not to hit it proves little.
AG_SRC = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
ok("auth/the worker's code contains no road to the word 'stored'",
   '"stored"' not in AG_SRC and "'stored'" not in AG_SRC)

# ============ 9. EVERY VERB A WORKFLOW CAN NAME HAS A HAND, AS A SET ======================
# Step 10B's "unreachable code paths", from the side it actually bites: a capability that
# exists in core.py and has no verb is a capability no workflow can reach, and a verb a
# drafted workflow emits with no hand in _one_step is a workflow that fails on that step.
# Both directions are checked as sets, so the next capability or the next drafted verb fails
# here instead of in a run on a real portal.
import re as _re                                                        # noqa: E402
import discover as _disc                                                # noqa: E402


class _FakeAgent(object):
    """Enough of an Agent to call _one_step for a refusal that happens before any browser
    call. A refusal that needed a browser to prove would not be a refusal worth having."""


def _step_why():
    """The load refusal for a consequential step, as a person would read it."""
    try:
        profiles.load({"slug": "stepwhy", "name": "Step why", "hosts": ["supplier.test"],
                       "workflows": {"w": {"class": "inspect",
                                           "steps": [{"do": "upload"}]}}})
    except profiles.ProfileError as e:
        return str(e)
    return ""

VERBS = set(_re.findall(r'if do == "([a-z_]+)"', AG_SRC))
ok("verbs/the step verbs are readable from the loop", len(VERBS) >= 8, str(sorted(VERBS)))

FOUND = {"url": "https://supplier.test/list", "title": "List", "can_read": True,
         "can_search": True, "pagination": True, "pagination_control": "Next",
         "needs_a_person": {}}
drafted = set()
for w in (_disc.draft_workflows(FOUND).get("workflows") or {}).values():
    for s in (w.get("steps") or []):
        drafted.add(str(s.get("do")))
ok("verbs/discovery drafts at least three kinds of step", len(drafted) >= 3, str(sorted(drafted)))
ok("verbs/EVERY verb a drafted workflow emits has a hand in the worker",
   not (drafted - VERBS), "no hand for: %s" % sorted(drafted - VERBS))

# The other direction. A core capability with no verb is unreachable; the exceptions are named
# here with the reason, so the list is a decision and not an oversight.
CORE_SRC = open(os.path.join(HERE, "core.py"), encoding="utf-8").read()
CAPS = set(_re.findall(r"def (hands_[a-z_]+|eyes_[a-z_]+)\(", CORE_SRC))
# The only capability with no verb, and the reason it has none: a popup is followed inside
# hands_click's own step. Everything else is declarable, which is what the loop enforces.
NOT_A_VERB = {"hands_click_popup",
              # the eyes the "read" and "discover" verbs use, never declared on their own
              "eyes_where", "eyes_controls", "eyes_tables", "eyes_headings", "eyes_state",
              "eyes_text",
              # DELIBERATELY REACHABLE BY NOTHING. A step runs before anybody approves, so an
              # upload cannot be a step; the approved path presses one named control and
              # cannot carry out an upload. Until an approved-upload path exists there is
              # nowhere legitimate for a verb to live, and the capability stays unreachable
              # rather than reachable without an approval.
              "hands_upload"}
VERB_OF = {"hands_goto": "goto", "hands_click": "click", "hands_type": "type",
           "hands_select": "select", "hands_download": "download",
           "eyes_screenshot": "screenshot", "eyes_pages": "read_pages",
           "hands_scroll": "scroll", "hands_press": "press",
           "hands_back": "back", "hands_tab": "tab", "eyes_tree": "tree"}
orphan = sorted(c for c in CAPS
                if c not in NOT_A_VERB and VERB_OF.get(c) not in VERBS)
ok("verbs/every capability in core is either a verb or named as deliberately not one",
   not orphan, "unreachable: %s" % orphan)
# research.py is the one module with no verb AND no exception, and that is reported rather
# than hidden: it needs a search host the owner has not named, so nothing may call it yet.
ok("verbs/research is reachable by nothing, which is the recorded blocker",
   "research" not in AG_SRC, "")

# ============ 10. A STEP CANNOT BE A CONSEQUENTIAL ACTION ==================================
# The hole a commit security review found within the hour of it being written: `upload` had
# been added to the step loop, and `steps` run BEFORE anybody approves anything. Asserted from
# both ends, as sets, because this control has to hold for the verb nobody has invented yet.
ok("step/the safe step verbs are an ALLOWLIST, so a new verb must be classified to be usable",
   isinstance(profiles.SAFE_STEP_VERBS, tuple) and len(profiles.SAFE_STEP_VERBS) >= 10,
   str(profiles.SAFE_STEP_VERBS))
ok("step/no verb the system classifies as consequential is in it",
   not [v for v in profiles.CONSEQUENTIAL_CLASSES if v in profiles.SAFE_STEP_VERBS],
   str([v for v in profiles.CONSEQUENTIAL_CLASSES if v in profiles.SAFE_STEP_VERBS]))
ok("step/upload above all is not a safe step verb",
   "upload" not in profiles.SAFE_STEP_VERBS and 'do == "upload"' not in AG_SRC, "")

# EVERY consequential class, as a step, is refused AT LOAD. One loop, so the next class added
# to CONSEQUENTIAL_CLASSES is covered by arriving there.
got_in = []
for cls in profiles.CONSEQUENTIAL_CLASSES:
    try:
        profiles.load({"slug": "stepcheck", "name": "Step check",
                       "hosts": ["supplier.test"],
                       "workflows": {"w": {"class": "inspect",
                                           "steps": [{"do": cls, "path": "/etc/passwd"}]}}})
        got_in.append(cls)
    except profiles.ProfileError:
        pass
ok("step/EVERY consequential verb is refused in steps, at load, over the whole class set",
   not got_in, str(got_in))
ok("step/and the refusal says where a consequential action belongs instead",
   "where a person decides on it" in _step_why(), _step_why()[:120])
ok("step/a profile of ordinary safe steps still loads",
   profiles.load({"slug": "stepok", "name": "Step ok", "hosts": ["supplier.test"],
                  "workflows": {"w": {"class": "inspect",
                                      "steps": [{"do": "goto"}, {"do": "read_pages"},
                                                {"do": "scroll"}, {"do": "discover"}]}}}
                 )["workflows"]["w"]["class"] == "inspect", "")

# Pressing Enter is a submit on a focused form, so it is refused as a step the same way --
# checked through the step loop itself, with no browser, because the refusal happens before
# anything is sent.
_pressed = agent.Agent.__dict__["_one_step"](
    _FakeAgent(), None, {"slug": "x", "hosts": ["supplier.test"]},
    {"do": "press", "keys": "Enter"})
ok("step/a step that presses Enter is refused before anything is sent",
   _pressed is not None and _pressed.ok is False and "a submission is a person's decision"
   in _pressed.detail, str(_pressed)[:140])

shutil.rmtree(PDIR, ignore_errors=True)

httpd.shutdown()
print()
for f in FAIL:
    print("   FAIL  " + f)
print("=" * 96)
print("PHASE 13 WORKER AGENT: %d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
