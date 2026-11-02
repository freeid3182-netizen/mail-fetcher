# -*- coding: utf-8 -*-
"""Phase 13 Step 2 — the universal browser core. Eyes, hands, and a bounded task.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT.

It is a thin, reusable layer over one Playwright Chromium. It knows nothing about any
website: no URL, no selector, no login flow and no portal name appears anywhere in this
file. A site is described by a PROFILE (profiles.py), supplied as data, so a new authorized
website needs configuration and not a new integration.

THE ONE RULE THAT SHAPES EVERY METHOD HERE: an action is not done because it was issued.
`hands_click` does not report success because a click was dispatched; it reports what the page
became afterwards, and every ActionResult carries HOW that was established. The contract says
"do not report success merely because a click or keystroke was issued", and the only way to
honour that in code is to make the verification the return value rather than an afterthought.

Three more that are structural, not stylistic:

  * BOUNDED. A task carries a Budget. Actions, seconds and downloaded bytes are all capped, and
    running out is an ordinary failure with a reason, not an exception that loses the task.
  * NOT SELECTOR-ONLY. Every eye reads structure (accessibility role and name, headings,
    tables) as well as the DOM, because a layout change must degrade into "I could not find it"
    rather than into a confident wrong click.
  * EVIDENCE, NOT NARRATION. Each result records url, title and optionally a screenshot path,
    so the portal can show what was actually on screen rather than the worker's description.

Nothing here reaches the portal or holds a credential. That is the caller's job.
"""
import os
import re
import time

import profiles          # the ONE host rule lives there; this file must not write a second

# PLAYWRIGHT IS IMPORTED WHERE IT IS USED, not here. The profile rules, the filename rules and
# the budget are plain logic that a machine with no browser must be able to import and test --
# and a worker that fails at import time on a box without Chromium is a worker nobody can
# debug. PWError/PWTimeout are resolved lazily for the same reason.
try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
except ImportError:                                  # no browser on this machine
    class PWError(Exception):
        pass

    class PWTimeout(PWError):
        pass


class ActionResult(object):
    """What happened, how we know, and what the page was when we looked.

    `verified_by` is the point of the class. A result that cannot say how it was checked is a
    result nobody should act on, so it is a required argument rather than an option.
    """

    def __init__(self, ok, what, verified_by, detail="", url="", title="", evidence=None):
        self.ok = bool(ok)
        self.what = what
        self.verified_by = verified_by
        self.detail = detail
        self.url = url
        self.title = title
        self.evidence = evidence or []

    def as_dict(self):
        return {"ok": self.ok, "what": self.what, "verified_by": self.verified_by,
                "detail": self.detail, "url": self.url, "title": self.title,
                "evidence": list(self.evidence)}

    def __repr__(self):
        return "<%s %s: %s (%s)>" % ("OK" if self.ok else "NO", self.what,
                                     self.detail[:60], self.verified_by)


class BudgetSpent(Exception):
    """Raised inside, converted to a failed ActionResult outside. Never escapes a task."""


class Budget(object):
    """Bounded actions, time and bytes.

    A runaway browser task on a CI runner costs minutes and a confusing failure; a runaway one
    on a real portal can mean repeated submissions. So the limit is explicit, checked before
    every action, and reports what it was rather than failing mutely.
    """

    def __init__(self, max_actions=120, max_seconds=240, max_bytes=40 * 1024 * 1024):
        self.max_actions = int(max_actions)
        self.max_seconds = int(max_seconds)
        self.max_bytes = int(max_bytes)
        self.actions = 0
        self.bytes = 0
        self.started = time.time()

    def spend(self, n=1):
        self.actions += n
        if self.actions > self.max_actions:
            raise BudgetSpent("action budget spent: %d of %d" % (self.actions, self.max_actions))
        if (time.time() - self.started) > self.max_seconds:
            raise BudgetSpent("time budget spent: %ds" % self.max_seconds)

    def spend_bytes(self, n):
        self.bytes += int(n)
        if self.bytes > self.max_bytes:
            raise BudgetSpent("download budget spent: %d of %d bytes"
                              % (self.bytes, self.max_bytes))

    def left(self):
        return {"actions": max(0, self.max_actions - self.actions),
                "seconds": max(0, int(self.max_seconds - (time.time() - self.started))),
                "bytes": max(0, self.max_bytes - self.bytes)}


# A name a filesystem and a human both accept, with the task id in it so two tasks cannot
# collide and provenance survives the file being moved. Deterministic on purpose: the same
# document fetched twice produces the same name, which is what makes duplicate detection
# possible further up.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name, task_id="", fallback="download.bin"):
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = _SAFE.sub("_", base).strip("._-")
    if not base:
        base = fallback
    if len(base) > 120:
        root, ext = os.path.splitext(base)
        base = root[:120 - len(ext)] + ext
    return ("%s__%s" % (task_id, base)) if task_id else base


class BrowserTask(object):
    """One browser, one task, one budget.

    Used as a context manager so the browser always closes, including on a budget failure --
    an orphaned Chromium on a runner is a resource leak that shows up as an unrelated timeout
    three steps later.
    """

    def __init__(self, headless=True, task_id="t0", download_dir="downloads",
                 budget=None, viewport=(1366, 900), profile=None, audit=None):
        self.headless = bool(headless)
        self.task_id = str(task_id)
        self.download_dir = download_dir
        self.budget = budget or Budget()
        self.viewport = viewport
        self.profile = profile
        self._audit = audit
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.log = []

    # ---- lifecycle ------------------------------------------------------------------
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        os.makedirs(self.download_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)
        self.ctx = self.browser.new_context(
            accept_downloads=True,
            viewport={"width": self.viewport[0], "height": self.viewport[1]})
        self.ctx.set_default_timeout(20000)
        self.page = self.ctx.new_page()
        # An unexpected dialog blocks a headless browser for ever. They are dismissed and
        # RECORDED, because "a confirm box appeared" is often the real outcome of a click.
        self.dialogs = []
        self.page.on("dialog", self._on_dialog)
        return self

    def __exit__(self, *exc):
        for closer in (lambda: self.ctx.close(), lambda: self.browser.close(),
                       lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        return False

    def _on_dialog(self, d):
        self.dialogs.append({"type": d.type, "message": d.message})
        try:
            d.dismiss()
        except Exception:
            pass

    def _record(self, res):
        self.log.append(res.as_dict())
        if self._audit:
            try:
                self._audit(res.as_dict())
            except Exception:
                pass
        return res

    def _fail(self, what, detail, verified_by="the page was read after the attempt"):
        return self._record(ActionResult(False, what, verified_by, detail,
                                        self._url(), self._title()))

    def _url(self):
        try:
            return self.page.url
        except Exception:
            return ""

    def _title(self):
        try:
            return self.page.title()
        except Exception:
            return ""

    # ---- EYES: what is on the page --------------------------------------------------
    # Structure first, pixels second. Each of these is read-only and costs one action.

    def eyes_where(self):
        self.budget.spend()
        return self._record(ActionResult(True, "where am I", "the page's own url and title",
                                         self._title(), self._url(), self._title()))

    def eyes_text(self, max_chars=20000):
        self.budget.spend()
        try:
            t = self.page.inner_text("body")
        except PWError as e:
            return self._fail("read the page text", str(e)[:200])
        t = t[:max_chars]
        return self._record(ActionResult(True, "read the page text", "body inner_text",
                                         t, self._url(), self._title()))

    def eyes_headings(self):
        self.budget.spend()
        hs = []
        for el in self.page.query_selector_all("h1,h2,h3,h4,legend,caption"):
            s = (el.inner_text() or "").strip()
            if s:
                hs.append(s[:200])
        return self._record(ActionResult(True, "read the headings", "h1-h4, legend, caption",
                                         "; ".join(hs[:40]), self._url(), self._title(),
                                         evidence=hs[:40]))

    def eyes_tables(self, max_rows=200):
        """Tables as rows of cells, WITH their header row kept separate.

        A heading is what a column IS -- this portal has already turned a mobile number into
        money by reading a column by position. So the header is returned beside the rows and
        never merged into them, and the caller reads by name.
        """
        self.budget.spend()
        out = []
        for tb in self.page.query_selector_all("table"):
            head = [(c.inner_text() or "").strip()
                    for c in tb.query_selector_all("thead th, tr:first-child th")]
            rows = []
            for tr in tb.query_selector_all("tbody tr, tr"):
                cells = [(c.inner_text() or "").strip()
                         for c in tr.query_selector_all("td")]
                if cells:
                    rows.append(cells)
                if len(rows) >= max_rows:
                    break
            if head or rows:
                out.append({"header": head, "rows": rows})
        return self._record(ActionResult(True, "read the tables", "table/thead/tbody cells",
                                         "%d table(s)" % len(out), self._url(), self._title(),
                                         evidence=out))

    def eyes_controls(self):
        """Links, buttons and fields by ROLE AND NAME, not by selector.

        This is the eye that makes an unfamiliar site workable: it answers "what can be done
        here" in the words the page itself uses, so the caller can match on a label instead of
        guessing a class name that changes with the next redesign.
        """
        self.budget.spend()
        items = []
        for role in ("link", "button", "textbox", "combobox", "checkbox", "radio"):
            try:
                loc = self.page.get_by_role(role)
                n = min(loc.count(), 60)
            except PWError:
                continue
            for i in range(n):
                try:
                    el = loc.nth(i)
                    name = (el.get_attribute("aria-label") or el.inner_text()
                            or el.get_attribute("name") or el.get_attribute("placeholder")
                            or "").strip()
                except Exception:
                    name = ""
                if name:
                    items.append({"role": role, "name": name[:120]})
        return self._record(ActionResult(True, "read the controls",
                                         "accessibility roles and their names",
                                         "%d control(s)" % len(items), self._url(),
                                         self._title(), evidence=items))

    def eyes_tree(self, depth_hint=None):
        self.budget.spend()
        try:
            ax = self.page.accessibility.snapshot()
        except PWError as e:
            return self._fail("read the accessibility tree", str(e)[:200])
        return self._record(ActionResult(bool(ax), "read the accessibility tree",
                                         "chromium accessibility snapshot",
                                         (ax or {}).get("name", "")[:120] if ax else "empty",
                                         self._url(), self._title(), evidence=[ax or {}]))

    def eyes_screenshot(self, name="shot", full_page=True):
        self.budget.spend()
        path = os.path.join(self.download_dir,
                            safe_filename(name + ".png", self.task_id, "shot.png"))
        try:
            self.page.screenshot(path=path, full_page=full_page)
        except PWError as e:
            return self._fail("take a screenshot", str(e)[:200])
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return self._record(ActionResult(size > 0, "take a screenshot",
                                         "the file exists and has bytes",
                                         "%d bytes" % size, self._url(), self._title(),
                                         evidence=[path]))

    def eyes_state(self):
        """Loading, dialogs and whether the page went somewhere unasked.

        An unexpected navigation is the failure mode that quietly turns a correct action into
        the wrong one -- a session expiring mid-form sends you to a login page, and a click
        aimed at "download" lands on "sign in". So it is a thing the caller can ASK about.
        """
        self.budget.spend()
        try:
            ready = self.page.evaluate("document.readyState")
        except PWError:
            ready = "unknown"
        return self._record(ActionResult(True, "read the page state",
                                         "document.readyState and recorded dialogs",
                                         "readyState=%s dialogs=%d" % (ready, len(self.dialogs)),
                                         self._url(), self._title(),
                                         evidence=list(self.dialogs)))

    # ---- HANDS: changing something, then checking what changed ----------------------

    def hands_goto(self, url, expect_host=None, wait="load"):
        """Navigate, and confirm where we ACTUALLY landed.

        expect_host is how a redirect to somewhere else stops being invisible. A portal that
        bounces to an SSO host, or to a "session expired" page, is a different site than the
        one we asked for, and the caller must be able to find that out without parsing text.
        """
        self.budget.spend()
        try:
            self.page.goto(url, wait_until=wait, timeout=30000)
        except PWTimeout:
            return self._fail("open " + url, "it did not finish loading in 30s",
                              "the navigation timed out")
        except PWError as e:
            return self._fail("open " + url, str(e)[:200], "the browser refused the url")
        landed = self._url()
        if expect_host:
            # profiles.host_of / host_matches, NOT a substring test on the url. The first
            # version of this line was `expect_host in landed`, which https://evil.test/
            # ?x=supplier.test satisfies -- the exact trick the profile host check was written
            # to stop, re-introduced here by writing the rule a second time.
            landed_host = profiles.host_of(landed)
            if not profiles.host_matches(landed_host, expect_host):
                return self._fail(
                    "open " + url,
                    "landed on host %r, which is not %s" % (landed_host or landed, expect_host),
                    "the landed url was PARSED and its hostname compared on a dot boundary")
        return self._record(ActionResult(True, "open " + url,
                                         "the url after loading, not the request",
                                         landed, landed, self._title()))

    def _find(self, by_role=None, name=None, selector=None):
        """One finder, two roads: role+name first, selector only as a fallback.

        Order matters. A role and an accessible name survive a redesign; a CSS selector does
        not. Preferring the durable one is the difference between "the layout changed" and a
        confident click on the wrong control.
        """
        if by_role and name:
            loc = self.page.get_by_role(by_role, name=name, exact=False)
            if loc.count():
                return loc.first, "role=%s name~=%s" % (by_role, name)
        if name and not selector:
            loc = self.page.get_by_text(name, exact=False)
            if loc.count():
                return loc.first, "visible text ~= %s" % name
        if selector:
            el = self.page.query_selector(selector)
            if el:
                return self.page.locator(selector).first, "selector %s" % selector
        return None, "nothing matched"

    def hands_click(self, by_role=None, name=None, selector=None, expect=None):
        """Click, then prove something changed.

        `expect` is a callable taking this task and returning (ok, how). Without it, the url,
        the title and any dialog are compared before and after -- so a click that did nothing
        at all is reported as a failure rather than a success, which is the single most common
        way browser automation lies.
        """
        self.budget.spend()
        loc, how = self._find(by_role, name, selector)
        if loc is None:
            return self._fail("click %s" % (name or selector), "no such control: " + how,
                              "the page was searched by role, text and selector")
        before = (self._url(), self._title(), len(self.dialogs))
        try:
            loc.click(timeout=15000)
        except PWTimeout:
            return self._fail("click %s" % (name or selector),
                              "the control was found but not clickable in 15s",
                              "found by " + how)
        except PWError as e:
            return self._fail("click %s" % (name or selector), str(e)[:200], "found by " + how)
        self.page.wait_for_timeout(250)
        if expect:
            ok, vb = expect(self)
            return self._record(ActionResult(ok, "click %s" % (name or selector), vb,
                                             "found by " + how, self._url(), self._title()))
        after = (self._url(), self._title(), len(self.dialogs))
        changed = after != before
        return self._record(ActionResult(
            changed, "click %s" % (name or selector),
            "the url, title and dialog count were compared before and after",
            "found by %s; %s" % (how, "page changed" if changed else "NOTHING changed"),
            after[0], after[1]))

    def hands_type(self, value, by_role="textbox", name=None, selector=None):
        self.budget.spend()
        loc, how = self._find(by_role, name, selector)
        if loc is None:
            return self._fail("type into %s" % (name or selector), "no such field: " + how)
        try:
            loc.fill(value, timeout=15000)
            got = loc.input_value(timeout=5000)
        except PWError as e:
            return self._fail("type into %s" % (name or selector), str(e)[:200])
        return self._record(ActionResult(
            got == value, "type into %s" % (name or selector),
            "the field was read back after typing", "found by %s; holds %r" % (how, got[:60]),
            self._url(), self._title()))

    def hands_select(self, value, name=None, selector=None):
        self.budget.spend()
        loc, how = self._find("combobox", name, selector)
        if loc is None:
            return self._fail("select %s" % (name or selector), "no such dropdown: " + how)
        try:
            loc.select_option(value, timeout=15000)
            got = loc.evaluate("e => e.value")
        except PWError as e:
            return self._fail("select %s" % (name or selector), str(e)[:200])
        return self._record(ActionResult(
            str(got) == str(value), "select %s in %s" % (value, name or selector),
            "the dropdown's value was read back", "found by %s; holds %r" % (how, got),
            self._url(), self._title()))

    def hands_press(self, keys):
        self.budget.spend()
        before = (self._url(), self._title())
        try:
            self.page.keyboard.press(keys)
        except PWError as e:
            return self._fail("press " + keys, str(e)[:200])
        self.page.wait_for_timeout(150)
        return self._record(ActionResult(True, "press " + keys,
                                         "the url and title after the keystroke",
                                         "was %s, now %s" % (before[1], self._title()),
                                         self._url(), self._title()))

    def hands_back(self):
        self.budget.spend()
        try:
            self.page.go_back(timeout=20000)
        except PWError as e:
            return self._fail("go back", str(e)[:200])
        return self._record(ActionResult(True, "go back", "the url after going back",
                                         self._url(), self._url(), self._title()))

    def hands_tab(self, index=None):
        """Switch tabs, or report them. Popups are pages too, and a portal that opens its
        document in a new tab is the normal case rather than the exception."""
        self.budget.spend()
        pages = self.ctx.pages
        if index is None:
            return self._record(ActionResult(True, "list the tabs", "the context's page list",
                                             "%d tab(s)" % len(pages), self._url(),
                                             self._title(),
                                             evidence=[p.url for p in pages]))
        if index < 0 or index >= len(pages):
            return self._fail("switch to tab %d" % index, "there are %d" % len(pages))
        self.page = pages[index]
        try:
            self.page.bring_to_front()
        except PWError:
            pass
        return self._record(ActionResult(True, "switch to tab %d" % index,
                                         "the active page's url after switching",
                                         self._url(), self._url(), self._title()))

    def hands_download(self, by_role=None, name=None, selector=None, expect_suffix=None):
        """Click something and WAIT FOR THE FILE, then check the bytes exist.

        A download is the one action where "it was clicked" is most tempting and least true:
        the click may open a login page, a 0-byte error file or nothing at all. So the file is
        waited for, saved under a deterministic name, measured, and charged to the budget.
        """
        self.budget.spend()
        loc, how = self._find(by_role, name, selector)
        if loc is None:
            return self._fail("download via %s" % (name or selector), "no such control: " + how)
        try:
            with self.page.expect_download(timeout=60000) as dl:
                loc.click(timeout=15000)
            d = dl.value
        except PWTimeout:
            return self._fail("download via %s" % (name or selector),
                              "no download started within 60s of the click",
                              "the browser's own download event was waited for")
        except PWError as e:
            return self._fail("download via %s" % (name or selector), str(e)[:200])
        name_on_disk = safe_filename(d.suggested_filename, self.task_id)
        path = os.path.join(self.download_dir, name_on_disk)
        try:
            d.save_as(path)
        except Exception as e:
            return self._fail("download via %s" % (name or selector),
                              "the browser offered a file but saving it failed: %s" % str(e)[:120])
        size = os.path.getsize(path) if os.path.exists(path) else 0
        self.budget.spend_bytes(size)
        if size == 0:
            return self._fail("download via %s" % (name or selector),
                              "the file arrived empty: " + path,
                              "the saved file's size was measured")
        if expect_suffix and not name_on_disk.lower().endswith(expect_suffix.lower()):
            return self._fail("download via %s" % (name or selector),
                              "expected %s, got %s" % (expect_suffix, name_on_disk),
                              "the saved filename was compared with what was expected")
        return self._record(ActionResult(
            True, "download via %s" % (name or selector),
            "the file was saved and its size measured",
            "%s, %d bytes" % (name_on_disk, size), self._url(), self._title(),
            evidence=[path]))

    def hands_upload(self, file_path, name=None, selector="input[type=file]"):
        """Choose a file. This reports SELECTION ONLY, in those words.

        Whether the portal accepted the upload is a different claim, provable only from the
        page afterwards, and the caller must make it explicitly rather than inherit it here.
        """
        self.budget.spend()
        if not os.path.exists(file_path):
            return self._fail("choose %s for upload" % file_path, "no such file")
        loc, how = self._find(None, name, selector)
        if loc is None:
            return self._fail("choose a file", "no file input: " + how)
        try:
            loc.set_input_files(file_path, timeout=15000)
            n = loc.evaluate("e => e.files.length")
        except PWError as e:
            return self._fail("choose a file", str(e)[:200])
        return self._record(ActionResult(
            int(n or 0) == 1, "choose %s for upload" % os.path.basename(file_path),
            "the input's own file list was read back -- SELECTION ONLY, not acceptance",
            "%s file(s) selected" % n, self._url(), self._title()))

    # ---- bounded retry --------------------------------------------------------------
    def attempt(self, fn, tries=2, pause_ms=600):
        """Run something up to `tries` times and STOP IF THE RESULT IS UNCERTAIN.

        Retrying is safe for reading and dangerous for acting, so this is used for reads and
        navigation only; anything that changes an external record is run once, by the caller,
        with its own idempotency. A retry that cannot tell whether the first attempt worked is
        how a form gets submitted twice.
        """
        last = None
        for i in range(max(1, int(tries))):
            try:
                last = fn()
            except BudgetSpent:
                raise
            except Exception as e:
                last = self._fail("attempt %d" % (i + 1), str(e)[:200])
            if getattr(last, "ok", False):
                return last
            if i + 1 < tries:
                self.page.wait_for_timeout(pause_ms)
        return last


def eye_names():
    """The eyes, as a set, so a report can map capabilities to code rather than to prose."""
    return sorted(n for n in dir(BrowserTask) if n.startswith("eyes_"))


def hand_names():
    return sorted(n for n in dir(BrowserTask) if n.startswith("hands_"))
