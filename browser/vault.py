# -*- coding: utf-8 -*-
"""Phase 13 Step 3A — credentials by KEY NAME, and redaction that is not optional.

THE RULE THIS FILE EXISTS TO KEEP: a secret's value never appears in a result, a log, an audit
row, a screenshot, an error message or a test fixture. The contract says so four times, and the
portal already works this way -- a connector names the KEYS it needs and the values live in the
guarded bl-connsec.php, redacted everywhere.

So this is not a credential store. It is a RESOLVER plus a REDACTOR:

  * resolve("WONDER_PASS") reads the value from the worker's environment -- a GitHub Actions
    secret, which is how the mail worker already receives MAILBOXES and PUSH_KEY. Nothing is
    written to disk, nothing is cached beyond the process, and no value is ever returned in
    anything that gets logged.
  * every value it resolves is registered for redaction, so the moment a credential is USED it
    becomes impossible to print. That ordering is the point: redaction that has to be
    remembered is redaction that will be forgotten on the day it matters.

WHAT IT WILL NOT DO. It will not accept a value in place of a key name, it will not ask anyone
for a password, and it will not read a credential out of a profile, a page, a mail or a tool
argument. A credential that arrives from outside the worker's own environment is not a
credential, it is an instruction from a stranger.
"""
import os
import re


class Vault(object):
    def __init__(self, env=None, prefix="BROWSER_CRED_"):
        self._env = env if env is not None else os.environ
        self._prefix = prefix
        self._secrets = set()          # values, for redaction only
        self._used = []                # KEY NAMES only, for the audit row

    # ---- resolution ---------------------------------------------------------------
    def resolve(self, key):
        """The value for a key name, or None. Registers it for redaction at the same moment."""
        k = str(key or "").strip()
        if not re.match(r"^[A-Z][A-Z0-9_]{2,48}$", k):
            raise ValueError("a credential KEY NAME is expected, not %r" % k)
        for name in (self._prefix + k, k):
            v = self._env.get(name)
            if v:
                self._secrets.add(v)
                if k not in self._used:
                    self._used.append(k)
                return v
        return None

    def have(self, keys):
        """Which of these key names are available, and which are missing.

        A workflow is refused for a MISSING CREDENTIAL before the browser opens, because
        finding out at the login page costs a browser launch and leaves a half-done task.
        """
        keys = [str(k).strip() for k in (keys or [])]
        missing = [k for k in keys if not self.resolve_quietly(k)]
        return {"needed": keys, "missing": missing, "ok": not missing}

    def resolve_quietly(self, key):
        """Is a value present, without registering or returning it."""
        k = str(key or "").strip()
        if not re.match(r"^[A-Z][A-Z0-9_]{2,48}$", k):
            return False
        return bool(self._env.get(self._prefix + k) or self._env.get(k))

    def used_keys(self):
        """The KEY NAMES used, for the audit row. Never the values."""
        return list(self._used)

    # ---- redaction ----------------------------------------------------------------
    def register(self, value):
        """Register something else sensitive -- a session cookie, a one-time token."""
        if value and len(str(value)) >= 4:
            self._secrets.add(str(value))
        return self

    def redact(self, text):
        """Replace every known secret with [REDACTED]. Longest first, so a secret that
        contains another is not half-replaced into something still readable."""
        s = str(text if text is not None else "")
        for v in sorted(self._secrets, key=len, reverse=True):
            if v and v in s:
                s = s.replace(v, "[REDACTED]")
        return s

    def redact_deep(self, obj):
        """Redact through dicts and lists, because an audit row is a dict and a result's
        evidence is a list of them."""
        if isinstance(obj, dict):
            return {k: self.redact_deep(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_deep(v) for v in obj]
        if isinstance(obj, str):
            return self.redact(obj)
        return obj

    def holds_secret(self, text):
        """For tests and for a last check before anything leaves the worker."""
        s = str(text if text is not None else "")
        return any(v in s for v in self._secrets if v)

    def count(self):
        return len(self._secrets)


# The fields a page may hold that must never be screenshotted or read back. Used by
# core.eyes_screenshot to mask before the pixels exist, rather than to blur after.
SENSITIVE_SELECTORS = ("input[type=password]", "input[name*=pass i]", "input[name*=otp i]",
                       "input[autocomplete=one-time-code]", "input[name*=captcha i]")
