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

import json         # noqa: E402
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
        "send_ack": {"class": "submit", "steps": [],
                     "act": {"do": "click", "role": "button",
                             "name": "Confirm"}},
    },
}


# A consequential workflow must declare the single control it would press -- profiles.load
# refuses one that does not, which is the point. This builds a valid workflow of any class so
# the loops below stay about the class and not about the boilerplate.
ACT = {"do": "click", "role": "button", "name": "Confirm"}


def wf(cls, **extra):
    w = {"class": cls, "steps": []}
    if cls in profiles.CONSEQUENTIAL_CLASSES:
        w["act"] = dict(ACT)
    w.update(extra)
    return w

p = profiles.load(GOOD)
ok("profile/a good one loads", p["slug"] == "fictional-supplier")
ok("profile/hosts are normalised", p["hosts"] == ["supplier.test", "vendor.test"],
   str(p["hosts"]))

# Every class, asserted over the SET. A new class added to either list without a thought about
# approval fails here instead of shipping.
gated, ungated = [], []
for cls in profiles.ALL_CLASSES:
    q = profiles.load(dict(GOOD, workflows={"w": wf(cls)}))
    (gated if q["workflows"]["w"]["approval_required"] else ungated).append(cls)
ok("profile/EVERY consequential class is approval-gated",
   sorted(gated) == sorted(profiles.CONSEQUENTIAL_CLASSES),
   "gated: %s" % sorted(gated))
ok("profile/EVERY read class is not gated, and is retryable",
   sorted(ungated) == sorted(profiles.READ_CLASSES)
   and all(profiles.load(dict(GOOD, workflows={"w": wf(c)}))
           ["workflows"]["w"]["retryable"] for c in profiles.READ_CLASSES),
   "ungated: %s" % sorted(ungated))

# A profile must not be able to switch an approval off. Over the whole consequential set.
refused = []
for cls in profiles.CONSEQUENTIAL_CLASSES:
    try:
        profiles.load(dict(GOOD, workflows={"w": wf(cls, approval_required=False)}))
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

# ---- THE HOST RULE, OVER EVERY SHAPE THAT HAS EVER BEEN USED TO BEAT ONE ----------------
# This set exists because the first version of this code had the rule written TWICE: a careful
# one here and `expect_host in landed_url` in core.hands_goto, which https://evil.test/
# ?x=supplier.test satisfies. The weaker copy is always the one that decides. So the shapes are
# asserted as a set, against the single shared rule, and core is checked for not having its own.
HOSTS = {
    "https://supplier.test/x": True,
    "https://SUPPLIER.TEST/x": True,                      # case
    "https://supplier.test:8443/x": True,                 # a port is not part of the host
    "https://a.supplier.test/x": True,
    "https://deep.a.vendor.test/x": True,
    "https://evilsupplier.test/x": False,                 # ends with the characters, not the host
    "https://supplier.test.attacker.test/x": False,
    "http://x@evil.test/supplier.test": False,            # userinfo before the real host
    "http://supplier.test@evil.test/": False,             # the expected name AS the userinfo
    "https://evil.test/?x=supplier.test": False,          # in the query
    "https://evil.test/#supplier.test": False,            # in the fragment
    "https://evil.test/supplier.test/doc": False,         # in the path
    # TRUE, and checked against Chromium rather than reasoned about: a browser rewrites the
    # backslash as a path separator, so this really does land on supplier.test with the path
    # /.evil.test/. Refusing it would be a false alarm, and a host rule that cries wolf gets
    # widened by whoever is on duty.
    "https://supplier.test\\.evil.test/": True,
    "https://ev\til.test/": False,                        # a tab browsers strip
    "file:///C:/windows/supplier.test": False,            # not http(s)
    "javascript:fetch('//supplier.test')": False,
    "data:text/html,<a href=//supplier.test>": False,
    "supplier.test/x": False,                             # no scheme is not a url
    "": False,
}
wrong = [u for u, want in HOSTS.items() if profiles.host_allowed(p, u) != want]
ok("profile/the host rule holds for all %d shapes, trick for trick" % len(HOSTS),
   not wrong, "wrong: %s" % wrong)
ok("profile/a host is parsed, not pattern-matched",
   profiles.host_of("https://supplier.test:8443/a?b=c#d") == "supplier.test"
   and profiles.host_of("ftp://supplier.test/") == ""
   and profiles.host_of("http://u:p@a.vendor.test/") == "a.vendor.test",
   profiles.host_of("http://u:p@a.vendor.test/"))
MATCH = {("supplier.test", "supplier.test"): True, ("a.supplier.test", "supplier.test"): True,
         ("evilsupplier.test", "supplier.test"): False, ("supplier.test", ""): False,
         ("", "supplier.test"): False, ("supplier.test", ".supplier.test"): True}
ok("profile/the dot-boundary match is right in both directions",
   not [k for k, v in MATCH.items() if profiles.host_matches(*k) != v],
   str([k for k, v in MATCH.items() if profiles.host_matches(*k) != v]))

# One rule, one implementation. A second copy in core is what this catches.
_CORE = open(os.path.join(HERE, "core.py"), encoding="utf-8").read()
ok("profile/core does NOT write its own host comparison",
   "expect_host.lower() not in" not in _CORE and "profiles.host_matches" in _CORE,
   "the landing check must call the shared rule")

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

        # THE REGRESSION FOR THE REAL BUG: the expected host appears in the query string of a
        # url served by somewhere else entirely. The substring version of this check passed it.
        rtrick = b.hands_goto(BASE + "/index.html?doc=supplier.test",
                              expect_host="supplier.test")
        ok("goto/the expected host inside the QUERY does not count as landing there",
           rtrick.ok is False and "127.0.0.1" in rtrick.detail, repr(rtrick))
        ok("goto/and the verification says the url was parsed",
           "PARSED" in rtrick.verified_by, rtrick.verified_by)
        rright = b.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        ok("goto/the genuine host still passes", rright.ok, repr(rright))

        # ---- THE PARSER MUST AGREE WITH THE BROWSER, SHAPE FOR SHAPE ----------------
        # The whole class of allow-list bypass is "my parser read a different host than the
        # browser will visit". So it is not reasoned about: Chromium is asked, and any
        # disagreement is a failure. This makes a parser differential impossible to
        # reintroduce by a later tidy-up of host_of().
        DIFFS = []
        for u in ("https://supplier.test\\.evil.test/", "http://supplier.test@evil.test/",
                  "https://supplier.test:8443/a?b=c#d", "https://ev\til.test/",
                  "https://SUPPLIER.TEST/x", "https://evil.test/?x=supplier.test",
                  "https://a.b.supplier.test/", "http://u:p@a.vendor.test/"):
            got = b.page.evaluate(
                "u => { try { return new URL(u).hostname } catch(e) { return 'ERR' } }", u)
            mine = profiles.host_of(u)
            if (got or "").lower() != mine:
                DIFFS.append("%r browser=%r mine=%r" % (u, got, mine))
        ok("host/the worker's parser agrees with Chromium on every shape", not DIFFS,
           "; ".join(DIFFS))

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

        # Masking is proved by the PIXELS, not by the argument having been passed. A password is
        # typed, then the same page is captured masked and unmasked: if the two images are
        # identical, nothing was painted over and the mask is decoration.
        b.hands_type("not-a-real-password", name="Password")
        masked = b.eyes_screenshot("masked", mask_sensitive=True)
        plain = b.eyes_screenshot("plain", mask_sensitive=False)
        mb = open(masked.evidence[0], "rb").read() if masked.ok else b""
        pb = open(plain.evidence[0], "rb").read() if plain.ok else b""
        ok("eyes/the sensitive fields are MASKED IN THE PIXELS, not after the fact",
           masked.ok and plain.ok and len(mb) > 0 and mb != pb,
           "masked %d bytes, plain %d bytes" % (len(mb), len(pb)))
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

        # ---- SCROLL. A portal that loads rows as you scroll reads as an empty list without
        # this, and "it scrolled" is only true if the position actually moved.
        b.hands_goto(BASE + "/pages1.html", expect_host="127.0.0.1")
        sc = b.hands_scroll("bottom")
        ok("scroll/it reaches the bottom and the position PROVES it",
           sc.ok and sc.evidence[0]["y"] > 0, repr(sc))
        up = b.hands_scroll("top")
        ok("scroll/and back to the top", up.ok and up.evidence[0]["y"] == 0, repr(up))
        el = b.hands_scroll(selector="#deep")
        ok("scroll/an element can be brought into view by name",
           el.ok and "#deep" in el.what, repr(el))
        bad = b.hands_scroll(selector="#there-is-no-such-thing")
        ok("scroll/and a scroll to something absent FAILS rather than reporting success",
           not bad.ok, repr(bad))

        # ---- PAGINATION, as one helper rather than the same loop in every workflow. Three
        # fixture pages: the last keeps its Next control and disables it.
        pg = b.eyes_pages(next_name="Next", max_pages=10)
        ok("pages/it reads every page of the list", pg.ok and "3 page(s)" in pg.what, repr(pg))
        ok("pages/and says WHY it stopped, so the end is not a guess",
           "end of the list" in pg.detail and "disabled" in pg.verified_by, repr(pg))
        ok("pages/every page's rows come back, not only the last one's",
           len(pg.evidence) == 3, str(len(pg.evidence)))
        b.hands_goto(BASE + "/pages1.html", expect_host="127.0.0.1")
        cap = b.eyes_pages(next_name="Next", max_pages=2)
        ok("pages/reaching the cap is a FAILURE, because the list was not read to its end",
           not cap.ok and "may be more" in cap.detail, repr(cap))

        # ---- POPUPS. hands_click waits for the current page, so a control that opens a new
        # window reads as a dead button and the real page is never looked at.
        b.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        pop = b.hands_click_popup(by_role="link", name="Open In New Tab",
                                  expect_host="127.0.0.1")
        ok("popup/a click that opens a new window is followed into it",
           pop.ok and "other.html" in pop.url, repr(pop))
        ok("popup/and the verification is the NEW window's own url",
           "new window's own url" in pop.verified_by, pop.verified_by)
        b.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        popbad = b.hands_click_popup(by_role="link", name="Open In New Tab",
                                     expect_host="supplier.test")
        ok("popup/a popup on a host the caller did not name is REFUSED, not switched to",
           not popbad.ok and "not supplier.test" in popbad.detail, repr(popbad))
        ok("popup/and the refusal leaves us on the page we were on",
           "index.html" in b.eyes_where().url, b.eyes_where().url)

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

    # ---- AUTHENTICATED PROFILE, through a real browser (§5.2/5.4).
    # The guards suite proves the FILE is encrypted and isolated. This proves the mechanism:
    # state exported from one browser is honoured by a second one that never saw the login.
    # It does NOT prove persistence across a machine -- the directory here lives as long as
    # this test does, and on the CI runner it dies with the job.
    import shutil
    import tempfile

    import session
    os.environ[session.KEY_ENV] = "a passphrase only the worker has"
    PDIR = tempfile.mkdtemp(prefix="atoz-prof-live-")
    PROF = profiles.load({"slug": "fixture", "name": "Local Fixture",
                          "hosts": ["127.0.0.1"],
                          "workflows": {"look": {"class": "inspect", "steps": [],
                                                 "means": "look at the page"}}})
    OTHERPROF = profiles.load({"slug": "elsewhere", "name": "Elsewhere",
                               "hosts": ["elsewhere.test"],
                               "workflows": {"look": {"class": "inspect", "steps": [],
                                                      "means": "look"}}})
    st = session.SessionStore(PDIR)
    with core.BrowserTask(headless=True, task_id="t3", download_dir=DL) as b3:
        b3.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        b3.page.evaluate("() => { document.cookie = 'SIGNEDIN=yes-it-is; path=/'; }")
        state = b3.export_state()
    ok("profile/the state exported from a browser names the host it authenticates",
       "127.0.0.1" in session.hosts_in(state), str(session.hosts_in(state)))
    st.save("fixture", state, PROF, verified_by="the cookie was set on the fixture page")
    with core.BrowserTask(headless=True, task_id="t4", download_dir=DL,
                          storage_state=st.load("fixture", PROF)) as b4:
        b4.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        got = b4.page.evaluate("() => document.cookie")
    ok("profile/a SECOND browser that never saw the login is signed in from the stored profile",
       "SIGNEDIN=yes-it-is" in got, got[:120])
    with core.BrowserTask(headless=True, task_id="t5", download_dir=DL) as b5:
        b5.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        bare = b5.page.evaluate("() => document.cookie")
    ok("profile/and a browser WITHOUT the profile is not signed in, so the test means something",
       "SIGNEDIN" not in bare, bare[:120])
    ok("profile/the exported state never reaches the task log",
       "yes-it-is" not in json.dumps(b3.log), "")
    shutil.rmtree(PDIR, ignore_errors=True)

    # ---- CAPABILITY DISCOVERY (§4.4/10.2). It must report what the page HAS and refuse to
    # decide what may be DONE to it: declaring a consequential control is declaring which
    # control an approval presses, and a page does not get to choose that about itself.
    import discover
    with core.BrowserTask(headless=True, task_id="t6", download_dir=DL) as b6:
        b6.hands_goto(BASE + "/index.html", expect_host="127.0.0.1")
        f = discover.inspect(b6, PROF)
        ok("disc/it finds the controls, the table and the search field that are really there",
           f["controls"] > 3 and f["tables"] >= 1 and f["can_search"], json.dumps(f)[:200])
        ok("disc/it sees the file input, so a workflow does not claim an upload blindly",
           f["can_upload"], str(f["file_inputs"]))
        ok("disc/it flags a download as a MAYBE, proved only by downloading one",
           f["may_have_downloads"] and "MAY be a download" in f["say"], f["say"][:140])
        ok("disc/every control that would act on the outside world is flagged for a person",
           f["needs_a_person"] and all(v for v in f["needs_a_person"].values()),
           str(sorted(f["needs_a_person"]))[:160])
        ok("disc/and it says so in the sentence, not only in a field",
           "need a person" in f["say"], f["say"][:160])

        d = discover.draft_workflows(f)
        ok("disc/the draft proposes only read-only workflows",
           d["workflows"] and all(w["class"] in profiles.READ_CLASSES
                                  for w in d["workflows"].values()),
           str([(k, v["class"]) for k, v in d["workflows"].items()]))
        ok("disc/it declares NO act control anywhere, because that is a person's decision",
           not [w for w in d["workflows"].values() if "act" in w],
           json.dumps(d["workflows"])[:160])
        ok("disc/and the payload itself says it is not a profile, where a reader will see it",
           "this_is_not_a_profile" in d and "slug" not in d and "hosts" not in d,
           str(sorted(d)))

        # A login screen first, because every other finding on it describes the login screen.
        b6.hands_goto(BASE + "/other.html", expect_host="127.0.0.1")
        g = discover.inspect(b6, PROF)
        ok("disc/a sign-in page is called one BEFORE anything else is reported",
           g["is_a_login_screen"] and g["say"].startswith("THIS IS A LOGIN SCREEN"),
           g["say"][:120])
        ok("disc/and the judgement is session.expiry_signs, not a second copy of that rule",
           "session.expiry_signs" in open(os.path.join(HERE, "discover.py"),
                                          encoding="utf-8").read())

        b6.hands_goto(BASE + "/pages1.html", expect_host="127.0.0.1")
        h = discover.inspect(b6, PROF)
        ok("disc/pagination is found, and named, so eyes_pages can be aimed at it",
           h["pagination"] and h["pagination_control"].lower() == "next",
           json.dumps(h)[:160])
        ok("disc/a host the profile does not allow is reported as such",
           discover.inspect(b6, OTHERPROF)["on_a_host_the_profile_allows"] is False)

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
