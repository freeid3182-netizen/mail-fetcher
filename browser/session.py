# -*- coding: utf-8 -*-
"""Phase 13 §5.2/5.4/5.6 — authenticated browser profiles, encrypted at rest and isolated.

WHAT THIS IS FOR. A portal login costs the owner an OTP and a CAPTCHA. Doing that at the start
of every task is the reason browser automation gets abandoned, so the authenticated state has
to survive the end of a task. What survives is cookies and storage -- that is to say, the
ability to BE the owner on that portal -- so this file is a credential store and is written
like one.

THREE RULES, each of which is the whole point of the file:

  1. ENCRYPTED AT REST, with a key that is never in the repo. Fernet (AES-128-CBC + HMAC) from
     `cryptography`, keyed by scrypt over BROWSER_PROFILE_KEY. A dependency for this is the
     right call: hand-rolling a cipher to avoid one is how credential stores get broken.

  2. ISOLATED BY PORTAL, AND PROVED, NOT PROMISED. A profile is saved only if every cookie in
     it belongs to a host that profile is allowed to touch. Without that check "one file per
     portal" is a filing convention: a Yahoo session saved under the supplier profile would be
     loaded into the supplier's browser, and the supplier's pages would be able to read it.
     The host rule is profiles.host_matches -- the same one, never a second copy.

  3. NOTHING LEAKS BY ACCIDENT. __repr__ says nothing about contents, the state never goes
     through the task log, and `summary()` is the ONLY thing meant for a screen or an audit
     row: counts, hosts and times, no values.

WHAT THIS FILE CANNOT DO, said here because the rest of the phase depends on believing it:
persistence needs a machine that still exists tomorrow. On the verified runtime -- an ephemeral
GitHub Actions runner -- the directory is destroyed with the job, so a profile saved in one run
is NOT there in the next. The code is correct and the capability is not available. It becomes
available when a persistent worker exists (`local_agent` is already one of the three words
bt_locations() accepts). Do not report it as working before then.
"""
import base64
import io
import json
import os
import time

import profiles

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    HAVE_CRYPTO = True
except ImportError:                                   # pragma: no cover - reported, not hidden
    HAVE_CRYPTO = False

KEY_ENV = "BROWSER_PROFILE_KEY"
# scrypt, not a bare sha256 of the passphrase: the key material is a human-chosen secret in an
# environment variable, so the slow KDF is the only thing standing between a copied file and a
# dictionary attack. The salt is fixed and in the open on purpose -- there is one store, so a
# per-file salt would buy nothing and would have to be stored beside the ciphertext anyway.
_SALT = b"atoz-browser-profile-v1"


class SessionError(RuntimeError):
    pass


def _slug(name):
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name).lower())
    s = s.strip("-")
    if not s:
        raise SessionError("a profile needs a name")
    return s[:60]


def _key():
    secret = os.environ.get(KEY_ENV, "")
    if not secret:
        raise SessionError(
            "%s is not set, so an authenticated profile cannot be encrypted and will not be "
            "written in the clear. Set it on the worker; it belongs in the worker's secrets, "
            "never in the repository." % KEY_ENV)
    if not HAVE_CRYPTO:
        raise SessionError(
            "the `cryptography` package is not installed, so profile state cannot be "
            "encrypted. It is not written unencrypted.")
    raw = Scrypt(salt=_SALT, length=32, n=2 ** 14, r=8, p=1).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(raw))


def hosts_in(state):
    """Every host the state would authenticate against: cookie domains and origins."""
    out = set()
    for c in (state or {}).get("cookies") or []:
        d = str(c.get("domain", "")).lstrip(".").lower()
        if d:
            out.add(d)
    for o in (state or {}).get("origins") or []:
        h = profiles.host_of(str(o.get("origin", "")))
        if h:
            out.add(h)
    return sorted(out)


def foreign_hosts(state, profile):
    """The hosts in this state that the profile is NOT allowed to touch.

    This is the isolation check, and it is a function so the test can assert over the whole set
    of hosts rather than over the one example that was in mind when it was written.
    """
    allowed = list((profile or {}).get("hosts") or [])
    bad = []
    for h in hosts_in(state):
        if not any(profiles.host_matches(h, a) for a in allowed):
            bad.append(h)
    return bad


class SessionStore(object):
    """One directory, one encrypted file per profile, nothing shared between them."""

    def __init__(self, directory):
        self.dir = directory

    def __repr__(self):
        # Deliberately says nothing about contents: a repr in a traceback is a log line.
        return "<SessionStore %d profile(s)>" % len(self.names())

    def _path(self, name):
        return os.path.join(self.dir, _slug(name) + ".enc")

    def names(self):
        try:
            return sorted(f[:-4] for f in os.listdir(self.dir) if f.endswith(".enc"))
        except OSError:
            return []

    def has(self, name):
        return os.path.exists(self._path(name))

    def save(self, name, state, profile, verified_by=""):
        """Write the authenticated state for ONE portal.

        `verified_by` is how we know the login actually worked -- a page title, a signed-in
        marker. A profile saved without it is a profile saved from a login page, which looks
        identical on disk and fails on the next task with no explanation.
        """
        if not str(verified_by).strip():
            raise SessionError(
                "a profile is saved only once the login has been VERIFIED. Say what proved it; "
                "an unverified save stores a logged-out session under a logged-in name.")
        bad = foreign_hosts(state, profile)
        if bad:
            raise SessionError(
                "this state carries cookies for %s, which profile %r is not allowed to touch. "
                "It is NOT saved: loading it would hand one portal another portal's session."
                % (", ".join(bad), (profile or {}).get("name", "?")))
        if not hosts_in(state):
            raise SessionError("this state authenticates nothing -- there is nothing to save")
        os.makedirs(self.dir, exist_ok=True)
        blob = _key().encrypt(json.dumps({
            "name": str(name), "profile": (profile or {}).get("name", ""),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "verified_by": str(verified_by)[:200],
            "hosts": hosts_in(state), "state": state,
        }).encode("utf-8"))
        path = self._path(name)
        tmp = path + ".tmp"
        with io.open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass          # Windows has no mode bits worth setting; the encryption is the guard
        return self.summary(name)

    def _read(self, name):
        path = self._path(name)
        if not os.path.exists(path):
            return None
        with io.open(path, "rb") as f:
            blob = f.read()
        try:
            return json.loads(_key().decrypt(blob).decode("utf-8"))
        except InvalidToken:
            raise SessionError(
                "profile %r cannot be decrypted with this %s. It is left alone: a wrong key is "
                "not a reason to delete somebody's session." % (name, KEY_ENV))

    def load(self, name, profile):
        """The state for a browser context, or None.

        The isolation check runs AGAIN on the way out. A file written by an older version, or
        by hand, or with a different profile's state in it, must not be loaded into this
        profile's browser just because its filename matches.
        """
        rec = self._read(name)
        if rec is None:
            return None
        bad = foreign_hosts(rec.get("state"), profile)
        if bad:
            raise SessionError(
                "profile %r on disk carries cookies for %s, which %r may not touch. It is NOT "
                "loaded." % (name, ", ".join(bad), (profile or {}).get("name", "?")))
        return rec.get("state")

    def summary(self, name):
        """What a screen, a report or an audit row may see. No values, ever."""
        rec = self._read(name)
        if rec is None:
            return {"name": str(name), "connected": False,
                    "say": "no authenticated profile is stored for this portal"}
        st = rec.get("state") or {}
        return {"name": rec.get("name", str(name)), "connected": True,
                "profile": rec.get("profile", ""),
                "saved_at": rec.get("saved_at", ""),
                "verified_by": rec.get("verified_by", ""),
                "hosts": rec.get("hosts", []),
                "cookies": len(st.get("cookies") or []),
                "origins": len(st.get("origins") or []),
                # AUTHENTICATION VALID IS NOT KNOWN FROM A FILE. §14.3 forbids a false
                # "connected", so the word stored here is deliberately "unknown" until a task
                # actually loads it and reads a signed-in page.
                "auth": "unknown",
                "say": "a profile is stored; whether the portal still accepts it is only known "
                       "when a task uses it"}

    def forget(self, name):
        """Disconnect or reset. One file, so one profile -- never a directory sweep."""
        path = self._path(name)
        if not os.path.exists(path):
            return {"ok": False, "error": "no stored profile called %r" % name}
        os.remove(path)
        return {"ok": True, "name": _slug(name),
                "say": "the stored session for %s is gone. The next task will need a fresh "
                       "login." % _slug(name)}


def expiry_signs(where_text, url, profile=None):
    """Does this page mean the session has gone? Returns (gone, why).

    Read from the page, not from an exception: a portal that bounces an expired session to its
    login screen returns HTTP 200 and every action afterwards fails for a reason that looks
    like a layout change. The login host counts as a sign in its own right, because a redirect
    off the profile's own hosts is the clearest form of it.
    """
    low = (where_text or "").lower()
    words = ("sign in", "signin", "log in", "login", "session has expired", "session expired",
             "your session", "please authenticate", "verify it's you", "verify its you",
             "two-step", "two-factor", "enter the code", "captcha")
    hit = [w for w in words if w in low]
    host = profiles.host_of(url or "")
    allowed = list((profile or {}).get("hosts") or [])
    off = bool(allowed) and host and not any(profiles.host_matches(host, a) for a in allowed)
    if off:
        return True, "the page is on %s, which this portal's profile does not list -- a login " \
                     "redirect" % host
    if hit:
        return True, "the page says %s" % ", ".join(repr(h) for h in hit[:3])
    return False, "nothing on the page says the session has gone"
