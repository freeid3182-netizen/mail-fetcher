# -*- coding: utf-8 -*-
"""Phase 13 Step 2 — targeted tests for the browser core and portal profiles.

SCOPE, per the contract's testing policy: only the code this step added, plus the smoke checks
that matter. No portal, no credential, no real host. The site under test is a local fixture
served from 127.0.0.1 and a set of fictional hosts that resolve nowhere.

THE PROPERTY THIS SUITE EXISTS FOR, above all others:

    AN ACTION IS NOT DONE BECAUSE IT WAS ISSUED.

So the fixture carries a button that is perfectly clickable and changes nothing, and the core
is required to call that a FAILURE. Every other assertion here is ordinary; that one is the
difference between automation and a confident liar.

Set-wise, never per member: every consequential class must be approval-gated, every read class
must not be, every eye and every hand must report how it verified itself.

Usage:  .venv-browser/Scripts/python mail-fetcher/browser/test_browser.py
"""
import functools
import http.server
import os
import socketserver
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import core          # noqa: E402
import profiles      # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if not detail else "%s  [%s]" % (name, detail))


# ============================================================ PROFILES (no browser needed)
GOOD = {
    "slug": "fictional-supplier",
    "name": "Fictional Supplier Portal",
    "hosts": ["supplier.test", ".vendor.test"],
    "credential_keys": ["SUPPLIER_USER", "SUPPLIER_PASS"],
    "workflows": {
        "list_pos": {"class": "inspect", "steps": [{"do": "goto", "path": "/"}]},
        "get_doc": {"class": "download", "steps": []},
        "send_ack": {"class": "submit", "steps": []},
    },
}

p = profiles.load(GOOD)
ok("profile/a good one loads", p["slug"] == "fictional-supplier")
ok("profile/hosts are normalised", p["hosts"] == ["supplier.test", "vendor.test"],
   str(p["hosts"]))

# Every class, asserted over the SET. A new class added to either list without a thought about
# approval fails here instead of shipping.
gated, ungated = [], []
for cls in profiles.ALL_CLASSES:
    q = profiles.load(dict(GOOD, workflows={"w": {"class": cls, "steps": []}}))
    (gated if q["workflows"]["w"]["approval_required"] else ungated).append(cls)
ok("profile/EVERY consequential class is approval-gated",
   sorted(gated) == sorted(profiles.CONSEQUENTIAL_CLASSES),
   "gated: %s" % sorted(gated))
ok("profile/EVERY read class is not gated, and is retryable",
   sorted(ungated) == sorted(profiles.READ_CLASSES)
   and all(profiles.load(dict(GOOD, workflows={"w": {"class": c, "steps": []}}))
           ["workflows"]["w"]["retryable"] for c in profiles.READ_CLASSES),
   "ungated: %s" % sorted(ungated))

# A profile must not be able to switch an approval off. Over the whole consequential set.
refused = []
for cls in profiles.CONSEQUENTIAL_CLASSES:
    try:
        profiles.load(dict(GOOD, workflows={"w": {"class": cls, "approval_required": False,
                                                  "steps": []}}))
    except profiles.ProfileError as e:
        if "may not change that" in str(e):
            refused.append(cls)
ok("profile/a file may NOT turn an approval off, for any consequential class",
   sorted(refused) == sorted(profiles.CONSEQUENTIAL_CLASSES),
   "refused: %d of %d" % (len(refused), len(profiles.CONSEQUENTIAL_CLASSES)))

BAD = {
    "no slug": dict(GOOD, slug=""),
    "a slug with spaces": dict(GOOD, slug="my portal"),
    "no name": dict(GOOD, name=""),
    "no hosts": dict(GOOD, hosts=[]),
    "a host that is a url": dict(GOOD, hosts=["https://supplier.test/login"]),
    "an unknown action class": dict(GOOD, workflows={"w": {"class": "frobnicate"}}),
    "steps that are not a list": dict(GOOD, workflows={"w": {"class": "inspect",
                                                             "steps": "goto"}}),
    "a credential VALUE instead of a key name": dict(GOOD,
                                                     credential_keys=["user=rahul@x.test"]),
    "a lowercase credential key": dict(GOOD, credential_keys=["supplier_pass"]),
}
rejected = []
for why, raw in BAD.items():
    try:
        profiles.load(raw)
    except profiles.ProfileError:
        rejected.append(why)
ok("profile/every malformed profile is refused with a reason",
   len(rejected) == len(BAD),
   "accepted: %s" % ", ".join(w for w in BAD if w not in rejected))

# host_allowed: the suffix trick is the one that matters.
HOSTS = {
    "https://supplier.test/x": True,
    "https://a.supplier.test/x": True,
    "https://deep.a.vendor.test/x": True,
    "https://evilsupplier.test/x": False,          # ends with the characters, not the host
    "https://supplier.test.attacker.test/x": False,
    "http://x@evil.test/supplier.test": False,     # userinfo before the real host
    "supplier.test/x": False,                      # no scheme is not a url
    "": False,
}
wrong = [u for u, want in HOSTS.items() if profiles.host_allowed(p, u) != want]
ok("profile/the host check holds for every shape, including the suffix trick",
   not wrong, "wrong: %s" % wrong)

# The intersection. A profile claiming `submit` cannot beat a grant of `inspect`.
okk, cls, need, why = profiles.allowed(p, "send_ack", ["inspect", "download"])
ok("profile/a profile cannot grant what the person was not granted",
   okk is False and cls == "submit" and need is True, "%s / %s" % (okk, why))
okk2, _c, need2, _w = profiles.allowed(p, "get_doc", ["inspect", "download"])
ok("profile/and a granted read workflow is allowed, ungated", okk2 is True and need2 is False)
okk3, _c, _n, why3 = profiles.allowed(p, "nonexistent", ["inspect"])
ok("profile/an unknown workflow is refused, not invented", okk3 is False and "no workflow" in why3)

# ============================================================ FILENAMES
NAMES = {
    "po-6204019486.pdf": "t7__po-6204019486.pdf",
    "../../etc/passwd": "t7__passwd",
    "C:\\Windows\\system32\\evil.exe": "t7__evil.exe",
    "   ": "t7__download.bin",
    "challan 09-2026.pdf": "t7__challan_09-2026.pdf",
}
bad = {k: core.safe_filename(k, "t7") for k in NAMES
       if core.safe_filename(k, "t7") != NAMES[k]}
ok("files/every name is made safe and deterministic, path tricks included", not bad, str(bad))
ok("files/the same input twice gives the same name",
   core.safe_filename("a b.pdf", "t7") == core.safe_filename("a b.pdf", "t7"))

# ============================================================ THE BROWSER
try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

if not HAVE_PW:
    ok("browser/playwright is installed", False, "pip install playwright && playwright install chromium")
else:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass          # a request log per asset buries the one line that matters

    handler = functools.partial(Handler, directory=os.path.join(HERE, "fixtures"))

    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, *a):
            pass

    httpd = Quiet(("127.0.0.1", 0), handler)
    PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    BASE = "http://127.0.0.1:%d" % PORT
    DL = os.path.join(HERE, "_test_downloads")

    with core.BrowserTask(headless=True, task_id="t1", download_dir=DL) as b:
        r = b.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        ok("goto/it opens and confirms where it landed", r.ok and "index.html" in r.url, repr(r))
        ok("goto/the verification names the url after loading, not the request",
           "after loading" in r.verified_by, r.verified_by)

        rbad = b.hands_goto(BASE + "/index.html", expect_host="supplier.test")
        ok("goto/a landing on the wrong host is a FAILURE",
           rbad.ok is False and "is not" in rbad.detail, repr(rbad))

        # ---- eyes
        ok("eyes/where", b.eyes_where().title == "Fictional Supplier Portal")
        rt = b.eyes_text()
        ok("eyes/text", rt.ok and "Purchase Orders" in rt.detail)
        rh = b.eyes_headings()
        ok("eyes/headings, all of them", rh.ok and len(rh.evidence) >= 5
           and "Documents" in rh.detail, rh.detail[:80])

        rtab = b.eyes_tables()
        t0 = (rtab.evidence or [{}])[0]
        ok("eyes/a table's header is kept SEPARATE from its rows",
           t0.get("header") == ["PO Number", "Site", "Amount", "Status"]
           and len(t0.get("rows") or []) == 3, str(t0)[:140])
        ok("eyes/so a column can be read by its heading, not its position",
           t0["rows"][0][t0["header"].index("Amount")] == "1,50,000.00",
           str(t0["rows"][0]))

        rc = b.eyes_controls()
        names = [c["name"] for c in rc.evidence]
        ok("eyes/controls are listed by role and NAME, not by selector",
           rc.ok and any("Search" in n for n in names)
           and any("Download PO" in n for n in names), str(names)[:160])
        ok("eyes/the accessibility tree comes back", b.eyes_tree().ok)
        rs = b.eyes_screenshot("portal")
        ok("eyes/a screenshot exists and has bytes", rs.ok and os.path.exists(rs.evidence[0]))
        ok("eyes/page state can be asked for", b.eyes_state().ok)

        # ---- hands: the headline property
        rdead = b.hands_click(by_role="button", name="Looks Important")
        ok("hands/A CLICK THAT CHANGES NOTHING IS A FAILURE",
           rdead.ok is False and "NOTHING changed" in rdead.detail, repr(rdead))
        ok("hands/and it says how that was established",
           "compared before and after" in rdead.verified_by, rdead.verified_by)

        rlive = b.hands_click(by_role="button", name="Search")
        ok("hands/a click that changes the page is a success",
           rlive.ok and rlive.title == "Search done", repr(rlive))

        rmiss = b.hands_click(by_role="button", name="A Button That Is Not There")
        ok("hands/a missing control is 'I could not find it', not a guess",
           rmiss.ok is False and "no such control" in rmiss.detail, repr(rmiss))

        rty = b.hands_type("6204019486", name="PO number")
        ok("hands/typing is verified by reading the field back",
           rty.ok and "6204019486" in rty.detail and "read back" in rty.verified_by, repr(rty))
        rsel = b.hands_select("2026-10", name="Period")
        ok("hands/a dropdown is verified by reading its value back",
           rsel.ok and "read back" in rsel.verified_by, repr(rsel))

        # ---- downloads
        rdl = b.hands_download(by_role="link", name="Download PO 6204019486",
                               expect_suffix=".txt")
        ok("download/the file is saved and its bytes measured",
           rdl.ok and os.path.exists(rdl.evidence[0])
           and os.path.getsize(rdl.evidence[0]) > 0, repr(rdl))
        ok("download/the saved name is deterministic and carries the task id",
           os.path.basename(rdl.evidence[0]).startswith("t1__"),
           os.path.basename(rdl.evidence[0]))
        rempty = b.hands_download(by_role="link", name="Download Empty File")
        ok("download/AN EMPTY FILE IS A FAILURE, not a download",
           rempty.ok is False and "empty" in rempty.detail, repr(rempty))
        rwrong = b.hands_download(by_role="link", name="Download PO 6204019486",
                                  expect_suffix=".pdf")
        ok("download/the wrong file type is a failure",
           rwrong.ok is False and "expected .pdf" in rwrong.detail, repr(rwrong))

        # ---- upload says SELECTION ONLY
        up = os.path.join(DL, "to-upload.txt")
        open(up, "w").write("x")
        rup = b.hands_upload(up, selector="#u")
        ok("upload/a chosen file is reported as SELECTION ONLY, in those words",
           rup.ok and "SELECTION ONLY" in rup.verified_by, rup.verified_by)

        # ---- tabs
        b.hands_click(by_role="link", name="Open In New Tab")
        rtabs = b.hands_tab()
        ok("tabs/a new tab is seen", rtabs.ok and len(rtabs.evidence) >= 2, repr(rtabs))
        rsw = b.hands_tab(1)
        ok("tabs/switching reports the url it switched to", rsw.ok and "other.html" in rsw.url)
        rsw0 = b.hands_tab(0)
        ok("tabs/switching back works", rsw0.ok and "index.html" in rsw0.url)
        ok("tabs/an index that does not exist is refused", b.hands_tab(99).ok is False)

        # ---- every result can say how it was verified
        nover = [e for e in b.log if not e.get("verified_by")]
        ok("evidence/EVERY result recorded says how it was verified", not nover,
           "%d without" % len(nover))
        ok("evidence/every result carries the url it happened on",
           all(("url" in e) for e in b.log))

        # ---- bounded retry on a read
        calls = []

        def flaky():
            calls.append(1)
            return b.eyes_where() if len(calls) > 1 else core.ActionResult(
                False, "pretend", "a deliberate first failure")
        rr = b.attempt(flaky, tries=3)
        ok("retry/a read is retried and then succeeds", rr.ok and len(calls) == 2,
           "%d call(s)" % len(calls))

    # ---- the budget, in its own task so the cap is reached cheaply
    with core.BrowserTask(headless=True, task_id="t2", download_dir=DL,
                          budget=core.Budget(max_actions=3)) as b2:
        b2.hands_goto(BASE + "/index.html")
        spent = False
        try:
            for _ in range(10):
                b2.eyes_where()
        except core.BudgetSpent as e:
            spent = "action budget spent" in str(e)
        ok("budget/the action cap bites, and says what it was", spent)
        ok("budget/what is left can be read", set(b2.budget.left()) ==
           {"actions", "seconds", "bytes"})

    httpd.shutdown()

# ============================================================ capability map, as a set
ok("map/the eyes are discoverable as a set", len(core.eye_names()) >= 7, str(core.eye_names()))
ok("map/the hands are discoverable as a set", len(core.hand_names()) >= 8, str(core.hand_names()))

print()
for f in FAIL:
    print("   FAIL  " + f)
print("=" * 96)
print("PHASE 13 STEP 2: %d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
