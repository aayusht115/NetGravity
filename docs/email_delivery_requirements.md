# Outbound email — what it needs before it can be handed out

NetGravity's Action Agent composes and sends the "we need this data from you"
emails. The composition, the recipients store, the dispatch log and the audit
trail are all built and tested. **The one thing missing is a credential**, and
it is missing deliberately: with none set, the sender runs in stub mode, logs
`[EMAIL STUB] would have emailed …` and sends nothing.

That default is why the feature is safe to demonstrate and **not yet safe to
hand out**: today it always reports success and never delivers.

---

## 1. What must be set

Four variables in `.env` (or the process environment, which wins over `.env`):

| Variable | Example | Why |
|---|---|---|
| `NETGRAVITY_SMTP_HOST` | `smtp.gmail.com` | **Setting this is what turns live mode on.** Until it is set, nothing else in this table is read. |
| `NETGRAVITY_SMTP_USERNAME` | `netgravity.reports@gmail.com` | The account that authenticates. |
| `NETGRAVITY_SMTP_PASSWORD` | *(16-character app password)* | For Gmail this **must be an App Password**, not the account password — see §2. |
| `NETGRAVITY_SMTP_FROM_ADDRESS` | `netgravity.reports@gmail.com` | The `From:` header. Falls back to the username; set it explicitly when sending from a shared mailbox. |

Optional:

| Variable | Default | Why you would change it |
|---|---|---|
| `NETGRAVITY_SMTP_PORT` | `587` | `587` is STARTTLS. Use `465` only with a relay that requires implicit TLS. |
| `NETGRAVITY_SMTP_USE_TLS` | `true` | `false` only for a local test relay with no TLS at all. Never against a real provider. |
| `NETGRAVITY_SMTP_TIMEOUT_SECONDS` | `20` | A relay that black-holes packets would otherwise hold the request thread for the OS TCP timeout, with a user watching a button that never returns. |
| `NETGRAVITY_EMAIL_STRICT` | `false` | **Set this to `true` before handing out.** See §4 — it is the difference between a failure that is reported and one that is swallowed. |
| `NETGRAVITY_INBOUND_EMAIL_DOMAIN` | *(unset)* | Needed only for reply-to threading (`ingest-{session}@domain`). Requires separate DNS/provider setup; blank is safe and simply omits the header. |

---

## 2. Gmail specifically

Gmail refuses SMTP authentication with an ordinary account password. You need
an **App Password**:

1. The account must have 2-Step Verification enabled.
2. Google Account → Security → 2-Step Verification → App passwords.
3. Generate one for "Mail". You get 16 characters.
4. Put that in `NETGRAVITY_SMTP_PASSWORD`, with spaces removed.

A Workspace account may have App Passwords disabled by policy, in which case
the administrator must either enable them or provide an SMTP relay.

**Gmail sending limits** are roughly 500 recipients/day for a consumer account
and 2,000 for Workspace. A data-request run that emails every supplier at once
can cross that, and Gmail's response is a temporary block on the account — so
for anything beyond demonstration, use a transactional provider (SendGrid,
Postmark, SES) rather than a mailbox. Swapping provider means rewriting one
method, `EmailSender._send_live`, and nothing else in the package.

---

## 3. How to verify

```bash
cd netgravity
PYTHONPATH=. python scripts/verify_email_delivery.py asishsatpathy99@gmail.com
```

The script reports the configuration it resolved, attempts one send, and says
plainly whether the message left the machine. Run it **before** the feature is
handed to anyone: a stubbed send and a real one are indistinguishable from the
screen, which is the entire risk this check exists to close.

---

## 4. The failure mode to close before handing out

By default a live send that fails is **caught, logged and degraded to a stub**.
The dispatch log records the failure, but the caller is told `sent=True` with
`stubbed=True`, and a screen that only checks `sent` will report success to a
user whose email never went.

That default is right for a demo — one bad address must not take down an
analysis run. It is wrong for production. Set:

```
NETGRAVITY_EMAIL_STRICT=true
```

and a failed live send raises `EmailSendError` instead of pretending.

**Partial refusals are already handled correctly** and do not need this flag:
if a relay accepts three addresses of four, the result names the one it
refused. `SMTP.send_message` raises only when *every* address is rejected, so
without that handling the one person whose address was mistyped would never be
asked and the screen would say everyone had been.

---

## 5. Status

| Piece | State |
|---|---|
| Draft composition, recipients store, dispatch log, audit trail | Built and tested |
| SMTP send with STARTTLS, partial-refusal reporting, timeout | Built |
| Verification script | `scripts/verify_email_delivery.py` |
| **Credential** | **Not set — this is what blocks live delivery** |
| `NETGRAVITY_EMAIL_STRICT=true` for production | Not set |
| Inbound reply threading | Needs DNS/provider setup, out of scope of this codebase |
