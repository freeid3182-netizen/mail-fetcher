# -*- coding: utf-8 -*-
"""Phase 13 Step 9A/9B/9C — the loop that turns a task into browser work, and stops in time.

WHAT THIS IS. The portal holds tasks; this takes one, works out which workflow of which portal
profile it means, carries out the READ steps, records a checkpoint after each step it VERIFIED,
and -- the moment it reaches anything consequential -- stops and asks a person. It then ends,
because waiting is the portal's job, not a process's. When the person approves, the next run
picks the task up from its checkpoint and carries out exactly the approved action, once.

THE FIVE THINGS IT WILL NOT DO, each of which is a rule rather than a preference:

  1. It will not decide that something needs no approval. gate.may_prepare answers whether the
     SAFE steps may run and says in as many words that it does not authorise the consequential
     one; gate.decide answers that, with a person's approval in hand; and the portal answers it
     again, under its own lock, before it hands out a token.
  2. It will not carry out a consequential action without a one-shot token from the portal.
     There is no code path to the hands for a consequential step that lacks one.
  3. It will not report success because a click happened. Every ActionResult carries
     verified_by, and a step whose result is not ok ends the run.
  4. It will not guess an outcome. If an approved action was carried out and the result could
     not be confirmed, it reports UNKNOWN -- which the portal treats as "may already have gone
     through" and never retries. Reporting FAILED there is the duplicate-submission bug.
  5. It will not take an instruction from a page. The instruction comes from the task record,
     the steps come from the profile, and nothing read from the browser becomes a step.

WHAT IT CANNOT YET DO, said plainly rather than hidden: it matches an instruction to a
workflow by the words in the instruction, not by understanding it.
shortcut: instruction-to-workflow matching is keyword-based, so an instruction that names no
workflow is reported BLOCKED rather than guessed at; upgrade it when the portal can hand the
worker a chosen workflow name alongside the task, which is a portal-side planning step and
belongs there, not here.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import audit        # noqa: E402
import core         # noqa: E402
import files        # noqa: E402
import gate         # noqa: E402
import profiles     # noqa: E402
import session      # noqa: E402
import vault as vaultmod  # noqa: E402


def load_profiles(directory):
    """Every portal profile on disk, by slug. A profile that does not load is SKIPPED and
    named -- never partially used, because a half-read profile is a half-read allow-list."""
    out, bad = {}, []
    if not os.path.isdir(directory):
        return out, bad
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            import json
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                p = profiles.load(json.load(f))
            out[p["slug"]] = p
        except Exception as e:
            bad.append("%s: %s" % (name, e))
    return out, bad


def pick_workflow(instruction, profile):
    """Which of this portal's workflows the instruction means, or None.

    Matched on the workflow's own name, split into words, so a profile that names its
    workflows after what they do is the thing being read -- not a list of synonyms kept here.
    None is a refusal, not a default: the task is reported BLOCKED and a person says which.
    """
    said = set(_words(instruction))
    best, score = None, 0
    for name, w in (profile.get("workflows") or {}).items():
        words = set(_words(name)) | set(_words(w.get("means", "")))
        hit = len(words & said)
        if hit > score:
            best, score = name, hit
    return best


def _words(s):
    out, cur = [], ""
    for ch in str(s or "").lower():
        if ch.isalnum():
            cur += ch
        else:
            if len(cur) > 2:
                out.append(cur)
            cur = ""
    if len(cur) > 2:
        out.append(cur)
    return out


def pick_portal(task, known):
    """The portal a task is about. The task's own slug first; failing that, nothing.

    DELIBERATELY NOT a guess from the instruction's words. "get it from the GST portal" must
    not select a profile because both strings contain "portal": a wrongly chosen profile is a
    wrongly chosen allow-list, and the allow-list is the only thing standing between this
    worker and an unverified host.
    """
    slug = str(task.get("portal") or "").strip().lower()
    return known.get(slug)


class Agent(object):
    def __init__(self, portal, profiles_dir=None, env=None, task_dir=None,
                 session_dir=None):
        self.portal = portal
        self.vault = vaultmod.Vault(env if env is not None else os.environ)
        portal.vault = self.vault
        self.profiles, self.bad_profiles = load_profiles(
            profiles_dir or os.path.join(HERE, "portals"))
        self.trail = audit.Trail(vault=self.vault)
        self.task_dir = task_dir or os.path.join(HERE, "_tasks")
        # Authenticated profiles, encrypted. One store, so a task never reads a directory it
        # was not given: see session.py for why the file is not the permission.
        self.sessions = session.SessionStore(
            session_dir or os.path.join(HERE, "_profiles"))

    # ---- the loop ----------------------------------------------------------------
    def run_once(self, limit=3):
        """Take what is queued and carry each as far as it may go. Returns one line per task."""
        said = []
        for t in self.portal.next_tasks(limit):
            try:
                said.append(self.run_task(t))
            except Exception as e:
                # A crash mid-task must leave a RECORD, not a silently abandoned task. The
                # state is reported before the exception is re-raised anywhere.
                self.portal.state(t["id"], "FAILED",
                                  "the worker stopped with %s" % type(e).__name__)
                said.append("%s FAILED (%s)" % (t["id"], type(e).__name__))
        return said

    def run_task(self, task):
        tid = task["id"]
        profile = pick_portal(task, self.profiles)
        if profile is None:
            self.portal.state(tid, "BLOCKED",
                              "no configured portal profile for %r. A portal is configured by "
                              "slug, with its verified hosts; it is never guessed from the "
                              "words of an instruction."
                              % (task.get("portal") or "(none given)"))
            return "%s BLOCKED: no profile" % tid

        workflow = pick_workflow(task.get("instruction", ""), profile)
        if workflow is None:
            self.portal.state(tid, "BLOCKED",
                              "this instruction does not name any workflow %s offers (%s)"
                              % (profile["slug"],
                                 ", ".join(sorted((profile.get("workflows") or {}).keys()))),
                              portal=profile["slug"])
            return "%s BLOCKED: no workflow" % tid

        grant = list(task.get("grant") or [])
        okg, why, facts = gate.may_prepare(profile, workflow, grant, vault=self.vault)
        if not okg:
            self.portal.state(tid, "BLOCKED", why, portal=profile["slug"], workflow=workflow)
            self.trail.append(audit.row(
                uid=task.get("uid", ""), task_id=tid, portal=profile["slug"],
                operation=workflow, action_class=facts.get("action_class") or "unknown",
                outcome="blocked", execution_location=self.portal.location, detail=why))
            return "%s BLOCKED: %s" % (tid, why)

        claimed = self.portal.claim(tid)
        if not claimed.get("ok"):
            # Somebody else took it, or a person cancelled it between the list and here.
            return "%s not claimed: %s" % (tid, claimed.get("error", ""))

        steps = list((profile["workflows"][workflow].get("steps") or []))
        return self._carry(task, profile, workflow, facts, steps)

    # ---- carrying the steps ------------------------------------------------------
    def _carry(self, task, profile, workflow, facts, steps):
        tid = task["id"]
        downloads = os.path.join(self.task_dir, tid)
        evidence = []
        # THE STORED SESSION, IF THERE IS ONE. A missing one is not an error: the first task
        # for a portal runs signed out, hits the login page and stops for the owner, which is
        # exactly §5.3. A session that cannot be DECRYPTED is also not an error here -- it is
        # reported and the task runs signed out, because a wrong key is not a reason to lose
        # the task.
        state = None
        try:
            state = self.sessions.load(profile["slug"], profile)
        except session.SessionError as e:
            self.portal.conn_say(profile["slug"], "expired", str(e)[:200])
        with core.BrowserTask(headless=True, task_id=tid, download_dir=downloads,
                             vault=self.vault, storage_state=state) as b:
            signed_in = False
            for i, step in enumerate(steps):
                res = self._one_step(b, profile, step)
                # §5.5. Read from the PAGE after every step, because a session dies between
                # two steps as readily as before the first, and every action after it fails
                # for a reason that reads like a layout change.
                if res is not None:
                    gone, why = session.expiry_signs(
                        (res.title or "") + " " + (res.detail or ""), res.url, profile)
                    if gone:
                        self.portal.need_auth(tid, profile["slug"], why)
                        self.trail.append(audit.row(
                            uid=task.get("uid", ""), task_id=tid, portal=profile["slug"],
                            operation=workflow, action_class=facts["action_class"],
                            outcome="waiting_for_auth",
                            execution_location=self.portal.location,
                            detail="the portal wants a login step a worker cannot do: " + why,
                            url=res.url))
                        return "%s WAITING_FOR_AUTH (%s)" % (tid, why)
                    if res.ok and state is not None and not signed_in:
                        # The stored session WORKED: a page on this portal's own host that is
                        # not a login screen. That, and only that, writes "verified" -- the
                        # portal refuses the word without the thing that proved it.
                        signed_in = True
                        self.portal.conn_say(profile["slug"], "verified",
                                             "reached %r with the stored session"
                                             % (res.title or res.url)[:120])
                if res is None:
                    self.portal.state(tid, "FAILED",
                                      "step %d asks for %r, which this worker has no hand for"
                                      % (i + 1, step.get("do")))
                    return "%s FAILED: unknown step %r" % (tid, step.get("do"))
                if not res.ok:
                    # A FAILED STEP ENDS THE RUN. Carrying on past a step that did not do what
                    # it claimed is how a worker ends up submitting on the wrong page.
                    self.portal.state(tid, "FAILED", "%s: %s" % (res.what, res.detail))
                    self.trail.append(audit.row(
                        uid=task.get("uid", ""), task_id=tid, portal=profile["slug"],
                        operation=workflow, action_class=facts["action_class"],
                        outcome="failed", execution_location=self.portal.location,
                        detail="%s: %s" % (res.what, res.detail), url=res.url))
                    return "%s FAILED at step %d: %s" % (tid, i + 1, res.detail)

                nxt = steps[i + 1].get("do", "") if i + 1 < len(steps) else "finish"
                self.portal.checkpoint(tid, res.what, res.verified_by, url=res.url,
                                       title=res.title, next_safe=nxt)
                if res.evidence:
                    evidence.extend(str(x) for x in res.evidence[:3])

                if step.get("do") == "download":
                    self._record_file(task, profile, step, res)

            # Every step done, and none of them consequential: the task is finished HERE,
            # with no approval, because nothing was asked of the portal that it could act on.
            if not facts["needs_approval"]:
                self.portal.state(tid, "COMPLETED",
                                  "%d step(s), each verified" % len(steps))
                self.trail.append(audit.row(
                    uid=task.get("uid", ""), task_id=tid, portal=profile["slug"],
                    operation=workflow, action_class=facts["action_class"],
                    outcome="completed", execution_location=self.portal.location,
                    detail="read-only workflow, %d steps" % len(steps),
                    evidence=evidence[:5]))
                return "%s COMPLETED (%d steps)" % (tid, len(steps))

            # Consequential. Everything safe is done; the exact content goes to a PERSON and
            # this run ends. Waiting is the portal's job -- a process that waits is a process
            # that loses the task when the runner is recycled.
            payload = self._payload(profile, workflow, b, task)
            self.portal.pend(tid, workflow, facts["action_class"], payload, shown=payload)
            self.trail.append(audit.row(
                uid=task.get("uid", ""), task_id=tid, portal=profile["slug"],
                operation=workflow, action_class=facts["action_class"],
                outcome="waiting_for_approval", execution_location=self.portal.location,
                detail="stopped before the consequential step; nothing was submitted",
                evidence=evidence[:5]))
            return "%s WAITING_FOR_APPROVAL (%s)" % (tid, facts["action_class"])

    def _one_step(self, b, profile, step):
        """One declared step, through the hands. Unknown verbs return None rather than being
        skipped: a skipped step is a workflow that quietly did less than it says it does."""
        do = str(step.get("do") or "")
        if do == "goto":
            host = (profile.get("hosts") or [None])[0]
            return b.hands_goto(step.get("url") or self._url(profile, step),
                                expect_host=host)
        if do == "click":
            return b.hands_click(by_role=step.get("role"), name=step.get("name"),
                                 selector=step.get("selector"))
        if do == "type":
            return b.hands_type(step.get("text", ""), name=step.get("name"),
                                selector=step.get("selector"))
        if do == "select":
            return b.hands_select(step.get("value", ""), name=step.get("name"),
                                  selector=step.get("selector"))
        if do == "download":
            return b.hands_download(by_role=step.get("role"), name=step.get("name"),
                                    selector=step.get("selector"))
        if do == "read":
            return b.eyes_tables() if step.get("what") == "tables" else b.eyes_text()
        if do == "screenshot":
            return b.eyes_screenshot(step.get("name", "step"))
        return None

    def _url(self, profile, step):
        host = (profile.get("hosts") or [""])[0]
        return "https://%s%s" % (host, step.get("path") or "/")

    def _payload(self, profile, workflow, b, task):
        """What the person will be shown and what the hash will cover: where we are, what the
        page says it is, and what the task asked for. Read off the page, so it is flattened by
        the portal before anybody reads it -- and short, because a payload nobody reads is an
        approval nobody gave."""
        where = b.eyes_where()
        return {
            "portal": profile["slug"],
            "workflow": workflow,
            "instruction": str(task.get("instruction") or "")[:200],
            "page_url": str(where.url or "")[:200],
            "page_title": str(where.title or "")[:120],
        }

    def _record_file(self, task, profile, step, res):
        """A download becomes a recorded document only after files.validate has asked its four
        questions. An unvalidated one is reported and NOT recorded -- the portal refuses it
        anyway, and both refusals are deliberate."""
        path = (res.evidence or [None])[0]
        if not path:
            return
        v = files.validate(path, expect_suffix=step.get("suffix"),
                           expect_period=step.get("period"),
                           expect_identifiers=step.get("identifiers") or ())
        prov = files.provenance(v, profile["slug"], task["id"], task.get("uid", ""),
                                res.url, original_name=os.path.basename(path),
                                portal_identifier=step.get("identifier", ""),
                                period=step.get("period", ""),
                                execution_location=self.portal.location)
        if not v["ok"]:
            self.portal.checkpoint(task["id"], "downloaded a file that did not validate",
                                   "files.validate said: " + v["why"], url=res.url,
                                   next_safe="a person decides whether to try again")
            return
        self.portal.file(task["id"], prov)


# ---- the approved half: a second run, after a person has decided --------------------

def resume_approved(agent, task, task_id=None):
    """Carry out ONE approved action and report what actually happened.

    THE ORDER HERE IS THE WHOLE POINT. It is m7s_gateway's order, plus the two steps a commit
    security review found missing -- and both of those were the dangerous kind, because the
    code around them looked complete:

      1. Decide locally, from the PROFILE, that this workflow may act at all and which single
         control it would press. The control is named in the profile. It used to be taken from
         the portal's JSON response, which made free text decide which button a consequential
         action presses.
      2. Ask the portal for a one-shot token. Everything it checks -- approved, unexpired,
         unchanged, unspent -- is checked there, under its lock, before this returns. Nothing
         below this line runs without it.
      3. GO BACK TO THE PAGE THAT WAS APPROVED, with the profile's host expected, so the
         landed host is parsed and compared on a dot boundary. It used to call eyes_where on a
         blank page and call that revalidation: an approved action carried out on whatever page
         happened to be open is still a wrong action, and on a fresh browser that page is none.
      4. Compare the page as it is NOW with the payload the person approved. A different url or
         a different title means the thing approved is not the thing in front of us, and
         nothing is pressed.
      5. Press the profile's one control, once.
      6. Report COMPLETED only if the page itself confirms it; UNKNOWN if it cannot be
         confirmed -- never FAILED, because the action may have gone through and FAILED invites
         a retry that would do it twice.

    `task` is the task record from the portal (portal slug, workflow, grant). A task_id may be
    passed separately for the odd caller that has only that.
    """
    tid = task_id or task["id"]
    profile = pick_portal(task, agent.profiles)
    workflow = str(task.get("workflow") or "") or pick_workflow(
        task.get("instruction", ""), profile or {"workflows": {}})
    if profile is None or not workflow or workflow not in (profile.get("workflows") or {}):
        return "%s not carried out: no profile or workflow for it" % tid
    w = profile["workflows"][workflow]
    act = w.get("act")
    if not w["approval_required"] or not isinstance(act, dict):
        # A read workflow has nothing to carry out, and a consequential one without an act
        # cannot load at all -- so this is unreachable unless the two drift apart.
        return "%s not carried out: %s declares no consequential step" % (tid, workflow)

    go = agent.portal.claim_exec(tid)
    if not go.get("ok"):
        return "%s not carried out: %s" % (tid, go.get("error", ""))
    token = go["exec_token"]
    approved = dict(go.get("payload") or {})

    # From here on an action MAY have happened, so there is no path that reports FAILED after
    # the control is pressed: it reports UNKNOWN and lets a person look.
    pressed = False
    try:
        with core.BrowserTask(headless=True, task_id=tid,
                              download_dir=os.path.join(agent.task_dir, tid),
                              vault=agent.vault) as b:
            back = b.hands_goto(approved.get("page_url") or "",
                                expect_host=(profile.get("hosts") or [None])[0])
            if not back.ok:
                agent.portal.result(tid, token, "FAILED",
                                    "could not return to the page this was approved on, so "
                                    "NOTHING was carried out: " + back.detail)
                return "%s not carried out: %s" % (tid, back.detail)

            now = agent._payload(profile, workflow, b, task)
            differs = [k for k in ("portal", "workflow", "page_url", "page_title")
                       if str(now.get(k) or "") != str(approved.get(k) or "")]
            if differs:
                agent.portal.result(tid, token, "FAILED",
                                    "the page is not the one that was approved (%s differ), "
                                    "so NOTHING was carried out. It must be approved again."
                                    % ", ".join(differs))
                return "%s not carried out: the page changed (%s)" % (tid, ",".join(differs))

            pressed = True
            done = b.hands_click(by_role=act.get("role"), name=act.get("name"),
                                 selector=act.get("selector"))
            if done.ok:
                agent.portal.result(tid, token, "COMPLETED", done.detail,
                                    evidence=done.evidence[:3])
                return "%s COMPLETED" % tid
            agent.portal.result(tid, token, "UNKNOWN",
                                "the action was carried out and the result could not be "
                                "confirmed: " + done.detail)
            return "%s UNKNOWN" % tid
    except Exception as e:
        # Before the control was pressed nothing happened, and saying UNKNOWN there would
        # freeze a task that is safe to approve again. After it, UNKNOWN is the only honest
        # word. The flag is the difference.
        if not pressed:
            agent.portal.result(tid, token, "FAILED",
                                "the worker stopped with %s BEFORE the approved action; "
                                "nothing was carried out" % type(e).__name__)
            return "%s not carried out (%s)" % (tid, type(e).__name__)
        agent.portal.result(tid, token, "UNKNOWN",
                            "the worker stopped with %s after pressing the approved control. "
                            "IT MAY HAVE GONE THROUGH." % type(e).__name__)
        return "%s UNKNOWN (%s)" % (tid, type(e).__name__)


def run_approved(agent, limit=3):
    """Everything a person has approved since the last run. The portal hands these back on a
    separate stage of btask_next, because an APPROVED task is not QUEUED work and must not be
    picked up by the ordinary loop."""
    said = []
    for t in agent.portal.next_tasks(limit, stage="approved"):
        said.append(resume_approved(agent, t))
    return said
