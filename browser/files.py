# -*- coding: utf-8 -*-
"""Phase 13 Step 4 — a downloaded file is not a document until something has checked it.

WHAT GOES WRONG WITHOUT THIS. A portal hands back an HTML error page named `challan.pdf`, or a
0-byte file, or the same challan twice under two names, or last month's challan for a month
nobody asked for. Each of those is a SUCCESSFUL download by any measure the browser can offer,
and each one becomes a wrong figure in front of a client if it is filed as what it claims.

So every file gets four separate questions asked of it, and the answers are kept:

  1. IS THERE ANYTHING THERE -- size, and not zero.
  2. IS IT WHAT IT CLAIMS -- the suffix AND the magic bytes. A PDF starts %PDF; a zip (xlsx and
     docx are zips) starts PK. An HTML error page served as a PDF is caught here and nowhere
     else, because it is the one failure that looks completely normal from the browser's side.
  3. IS IT THE RIGHT ONE -- the period and the identifiers the caller expected, matched against
     the filename and, where the caller supplies text, the content.
  4. HAVE WE SEEN IT BEFORE -- by sha256, and separately by the portal's own identifier.

THE RULE THAT SHAPES THE DUPLICATE HANDLING: §9 of the project state, "source files are never
modified, never overwritten, never deleted. A duplicate is marked, not removed." So nothing
here deletes or overwrites anything. A repeat arrives, is saved beside the first, and is
MARKED as a duplicate of it, with both hashes recorded. A silent merge of two files that might
differ is the one outcome this module exists to prevent.
"""
import datetime
import hashlib
import os
import re

# Enough to catch the failure that matters: something that is not the kind of file it claims.
# Not a full format library -- the question is "is this an error page pretending", not "is this
# a valid PDF 1.7".
MAGIC = {
    ".pdf": (b"%PDF",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
    ".xlsx": (b"PK\x03\x04",),
    ".docx": (b"PK\x03\x04",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}
# What an error page or a login redirect looks like when it arrives with a document's name.
NOT_A_DOCUMENT = (b"<!doctype html", b"<html", b"<HTML", b"<!DOCTYPE HTML")


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def validate(path, expect_suffix=None, expect_period=None, expect_identifiers=(),
             min_bytes=1, text=None):
    """Four questions, one answer. `ok` is false if any of them fails, and `why` says which.

    `text` is optional extracted text -- this module does not read PDFs, because the host has
    no pdftotext and the portal already has its own readers. When the caller has text, the
    period and identifier checks use it as well as the filename.
    """
    out = {"path": path, "ok": False, "why": "", "sha256": "", "bytes": 0,
           "suffix": os.path.splitext(path)[1].lower(), "checked": []}
    if not os.path.exists(path):
        out["why"] = "the file is not there"
        return out
    size = os.path.getsize(path)
    out["bytes"] = size
    out["checked"].append("exists")

    if size < max(1, int(min_bytes)):
        out["why"] = ("the file is %d bytes, which is not a document" % size)
        return out
    out["checked"].append("size")

    with open(path, "rb") as f:
        head = f.read(512)

    suffix = (expect_suffix or out["suffix"] or "").lower()
    if expect_suffix and out["suffix"] != expect_suffix.lower():
        out["why"] = "expected %s, got %s" % (expect_suffix, out["suffix"] or "no suffix")
        return out

    # An error page wearing a document's name. Checked before the magic bytes so the reason
    # reads usefully: "this is a web page" beats "bad magic".
    if suffix not in (".html", ".htm", ".txt", ""):
        low = head.lstrip()[:64].lower()
        if any(low.startswith(m.lower()) for m in NOT_A_DOCUMENT):
            out["why"] = ("it is an HTML page, not a %s -- most likely an error or a login "
                          "redirect saved under the document's name" % suffix)
            return out

    if suffix in MAGIC:
        if not any(head.startswith(m) for m in MAGIC[suffix]):
            out["why"] = ("the first bytes are not a %s (%r)" % (suffix, head[:8]))
            return out
        out["checked"].append("magic")

    hay = (os.path.basename(path) + "\n" + (text or "")).lower()
    if expect_period:
        p = str(expect_period).lower()
        alts = {p, p.replace("-", ""), p.replace("-", "/"), p.replace("-", "_")}
        if not any(a in hay for a in alts):
            out["why"] = ("the period %s is not in the filename or the text; this may be the "
                          "wrong reporting period" % expect_period)
            return out
        out["checked"].append("period")

    missing = [str(i) for i in (expect_identifiers or ()) if str(i).lower() not in hay]
    if missing:
        out["why"] = "expected identifier(s) not found: %s" % ", ".join(missing)
        return out
    if expect_identifiers:
        out["checked"].append("identifiers")

    out["sha256"] = sha256_of(path)
    out["checked"].append("sha256")
    out["ok"] = True
    out["why"] = "checked: " + ", ".join(out["checked"])
    return out


def provenance(validation, portal, task_id, uid, source_url, original_name="",
               portal_identifier="", period="", execution_location="background_ci"):
    """Where a file came from, kept with the file. Every field the contract's §4C names.

    This travels with the bytes. A document whose provenance is missing cannot be told apart
    later from one somebody dropped in a folder by hand, and that distinction is the whole
    value of fetching it automatically.
    """
    return {
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"),
        "portal": str(portal),
        "portal_identifier": str(portal_identifier or ""),
        "source_url": str(source_url or ""),
        "task_id": str(task_id),
        "uid": str(uid),
        "execution_location": str(execution_location),
        "original_name": str(original_name or ""),
        "stored_name": os.path.basename(validation.get("path") or ""),
        "bytes": int(validation.get("bytes") or 0),
        "sha256": str(validation.get("sha256") or ""),
        "period": str(period or ""),
        "validated": bool(validation.get("ok")),
        "validation": str(validation.get("why") or ""),
        "duplicate_of": "",
    }


def find_duplicate(prov, index):
    """Is this file already known? By CONTENT first, then by the portal's own identifier.

    Two answers, not one, because they mean different things:
      * same sha256  -> the same bytes. Certainly a duplicate.
      * same portal identifier, DIFFERENT bytes -> the portal has reissued the document. That
        is not a duplicate at all; it is a revision, and treating it as a duplicate would hide
        a changed challan. So it is reported separately and loudly.
    """
    h = prov.get("sha256")
    pid = (prov.get("portal_identifier") or "").strip().lower()
    for other in index or []:
        if h and other.get("sha256") == h:
            return {"kind": "same_bytes", "of": other.get("stored_name"),
                    "why": "identical sha256"}
    for other in index or []:
        if pid and (other.get("portal_identifier") or "").strip().lower() == pid:
            if other.get("sha256") and h and other["sha256"] != h:
                return {"kind": "reissued", "of": other.get("stored_name"),
                        "why": ("the portal's identifier %s was seen before with DIFFERENT "
                                "bytes -- this is a revision, not a copy" % pid)}
    return None


def mark_duplicate(prov, dup):
    """Marked, never removed. §9. The file stays on disk and keeps its own provenance."""
    p = dict(prov)
    p["duplicate_of"] = dup.get("of") or ""
    p["duplicate_kind"] = dup.get("kind") or ""
    p["duplicate_why"] = dup.get("why") or ""
    return p


_SAFE_PERIOD = re.compile(r"^\d{4}-\d{2}$")


def period_ok(p):
    """YYYY-MM or nothing. A period the worker cannot parse is a period it will not claim to
    have matched -- this is the field that decides which month's money a document is about."""
    return bool(_SAFE_PERIOD.match(str(p or "")))
