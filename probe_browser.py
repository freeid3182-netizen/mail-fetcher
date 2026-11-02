# PHASE 13 STEP 1 -- what can a CI runner's browser actually DO?
#
# Each check maps to a capability the Phase 13 contract requires, so the report can say
# "verified" or "not verified" per capability instead of per framework. Nothing here touches a
# portal, a credential or the business: one public page, one data: URL, one download.
#
# Run: python probe_browser.py [--headed]
import json
import os
import sys

from playwright.sync_api import sync_playwright

HEADED = "--headed" in sys.argv
TAG = "headed" if HEADED else "headless"
OUT = []


def say(capability, ok, detail=""):
    OUT.append((capability, bool(ok), str(detail)[:200]))
    print("%-34s %-12s %s" % (capability, "VERIFIED" if ok else "NO", str(detail)[:120]))


def main():
    os.makedirs("probe-download", exist_ok=True)
    with sync_playwright() as p:
        say("chromium launches (%s)" % TAG, True, "")
        b = p.chromium.launch(headless=not HEADED)
        say("browser version", True, b.version)

        # Isolated context per task -- Step 9F wants independent contexts.
        ctx = b.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()

        pg.goto("https://example.com", wait_until="load", timeout=30000)
        say("navigate a public page", pg.url.startswith("https://example.com"), pg.url)
        say("read title", bool(pg.title()), pg.title())

        # EYES, structural: visible text, headings, the DOM, and the accessibility tree.
        say("visible text", "Example Domain" in pg.inner_text("body"),
            pg.inner_text("body").split("\n")[0])
        say("dom query", len(pg.query_selector_all("a")) >= 1,
            "%d links" % len(pg.query_selector_all("a")))
        ax = pg.accessibility.snapshot()
        say("accessibility tree", isinstance(ax, dict) and bool(ax.get("role")),
            json.dumps(ax)[:90] if ax else "none")

        # EYES, visual.
        shot = "probe-%s.png" % TAG
        pg.screenshot(path=shot, full_page=True)
        say("screenshot", os.path.exists(shot) and os.path.getsize(shot) > 1000,
            "%d bytes" % (os.path.getsize(shot) if os.path.exists(shot) else 0))

        # HANDS: a form, typed into and read back. A data: URL keeps this off anyone's server.
        pg.goto("data:text/html,<form><input id=t name=t><select id=s>"
                "<option value=a>A<option value=b>B</select>"
                "<button id=go type=button onclick=\"document.title='clicked'\">Go</button></form>")
        pg.fill("#t", "AtoZ Commercial")
        pg.select_option("#s", "b")
        pg.click("#go")
        say("type into a field", pg.input_value("#t") == "AtoZ Commercial", pg.input_value("#t"))
        say("select a dropdown", pg.eval_on_selector("#s", "e => e.value") == "b", "")
        say("click changes page state", pg.title() == "clicked", pg.title())

        # Tabs, which the contract asks for by name.
        pg2 = ctx.new_page()
        pg2.goto("data:text/html,<h1>second tab</h1>")
        say("second tab", len(ctx.pages) == 2, "%d pages" % len(ctx.pages))
        pg2.close()

        # DOWNLOAD, with completion checked rather than assumed.
        pg.goto("data:text/html,<a id=d download=probe.txt "
                "href=\"data:text/plain,phase13-probe\">dl</a>")
        with pg.expect_download(timeout=20000) as dl:
            pg.click("#d")
        d = dl.value
        path = os.path.join("probe-download", d.suggested_filename or "probe.txt")
        d.save_as(path)
        ok = os.path.exists(path) and open(path).read().strip() == "phase13-probe"
        say("download, bytes verified", ok, path)

        # UPLOAD: the field accepts a file. Whether a portal ACCEPTED it is a different claim.
        with open("probe-upload.txt", "w") as f:
            f.write("x")
        pg.goto("data:text/html,<input id=u type=file>")
        pg.set_input_files("#u", "probe-upload.txt")
        say("file chosen for upload",
            pg.eval_on_selector("#u", "e => e.files.length") == 1, "selection only")

        ctx.close()
        b.close()

    with open("probe-%s.txt" % TAG, "w") as f:
        for cap, ok, detail in OUT:
            f.write("%s\t%s\t%s\n" % (cap, "VERIFIED" if ok else "NO", detail))
    bad = [c for c, ok, _ in OUT if not ok]
    print("\n%s: %d verified, %d not" % (TAG, len(OUT) - len(bad), len(bad)))
    if bad:
        print("not verified: " + ", ".join(bad))
        sys.exit(1)


main()
