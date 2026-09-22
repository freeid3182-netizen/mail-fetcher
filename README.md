# Mail fetcher

Reads mailboxes over IMAP and hands new messages to the AtoZ Commercial portal, which
cannot open a mailbox itself (its host blocks port 993).

It **only reads**. Mailboxes are opened read-only, messages are fetched with `BODY.PEEK`,
so nothing is marked read, moved, deleted, or sent. Stop the schedule and the mailboxes are
exactly as they were.

## Setup

Repository → Settings → Secrets and variables → Actions

**Variable** `PORTAL_URL` — `https://atozengineerings.com/billing/api.php`

**Secret** `PUSH_KEY` — from the portal: Mailboxes page → worker key. It can push mail and
nothing else: not quotations, not users, not saving.

**Secret** `MAILBOXES` — one JSON line per mailbox:

```json
[
 {"box":"SHY-Yahoo","host":"imap.mail.yahoo.com","user":"you@yahoo.com","pass":"app password"},
 {"box":"AtoZ-Gmail","host":"imap.gmail.com","user":"you@gmail.com","pass":"app password"}
]
```

`box` is the label the portal shows. It is half of each message's id, so **do not rename a
box** once it is running — old messages would come back looking new.

Passwords are **app passwords**, never the real one: Yahoo → Account Security → Generate
app password; Gmail → myaccount.google.com → Security → App passwords (needs 2-Step on).
Revoking one there cuts this worker off instantly.

## Running

Every 15 minutes, and Actions → Fetch mail → Run workflow for a run right now. The log says
what each mailbox did; a mailbox that fails does not stop the others.

The first run of a mailbox takes only the newest 15 messages, so the portal does not fill
up with years of old mail. After that it takes everything new, 40 per run at most.
