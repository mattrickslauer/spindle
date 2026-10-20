"""The one irreversible act in the product, behind a Protocol.

Everything else this system does can be undone. A wrong fact is superseded, a bad lead is
parked, a duplicated party is merged. **A sent email cannot be recalled**, and the whole
shape of `outbox` — one row per message, a unique idempotency key, an approver recorded
before the row exists — is built around that asymmetry. This module is where it actually
leaves the building.

## Why Amazon SES and not Gmail

Gmail was considered and rejected on three counts, all of them fatal rather than
inconvenient:

  * **Wrong layer.** A Gmail MCP server is a tool an *assistant* holds. The thing that
    needs to send here is a worker draining `outbox` — possibly in Lambda, possibly on a
    laptop at 3am — and it cannot borrow somebody's interactive OAuth session.
  * **Limits.** 500 messages/day on consumer Gmail, 2,000 on Workspace, and cold
    outreach from a personal mailbox is the fastest known way to have that mailbox
    suspended. Losing the label's actual email to a hackathon demo is not a trade worth
    making.
  * **No feedback loop.** Gmail will not tell you a message hard-bounced. SES publishes
    bounces and complaints to SNS, and those map exactly onto the `contact_route.state`
    values migration 018 already defined — `bounced` and `opted_out`. The route learns
    from the send, which is the same accumulation argument the `lesson` loop makes.

SES is also AWS, which keeps the deployment on one cloud and one IAM model.

### What changed on 2026-08-16, and which half of that argument survived

`GmailMailer` now exists below, so the section above needs qualifying rather than
deleting — because it was answering a different question than the one that has since been
asked.

It was asking *what should the platform send through*, and the answer is unchanged: SES,
for all three reasons, and the platform identity is what every free tenant sends on. What
`040_mailbox.sql` added is a second question — *what should a paying tenant send through*
— and there the three objections read differently. The worker is not borrowing somebody's
interactive session, it is replaying a refresh token that tenant granted for exactly this
purpose. The daily limit applies to a mailbox its owner chose to use, at a volume their
plan already caps well below it. And the suspension risk lands on a domain they own and
control rather than on a shared identity every other tenant depends on — which inverts the
third objection completely: sending a label's cold outreach from *our* domain is the
version where one tenant's mistake costs everybody.

The bounce feedback loop is the one real loss, and it is a loss. SES publishes bounces and
complaints to SNS and Gmail does not, so a tenant on their own mailbox will not have
`contact_route.state` learn from a hard bounce the way a platform-sent one does. Nothing
here compensates for that yet; it is recorded rather than solved.

### What changed on 2026-10-19: the platform transport is Resend

`SESMailer` is gone and `ResendMailer` carries the platform identity. The account never
left the SES sandbox, where every *recipient* must itself be a verified identity — so a
sign-in code could only reach an address somebody had clicked a link for in the AWS
console. Resend has no sandbox for a verified domain, and `spindle@mattrickslauer.com`'s
domain is verified there. The SES identity, DKIM and MAIL FROM records are left in place
(DNS is not torn down by this change); nothing sends through them.

The reasoning above about *why not Gmail for the platform* stands unchanged — it was an
argument for a transactional provider, and Resend is one. Resend also reports bounces and
complaints (by webhook rather than SNS); wiring them to `contact_route.state` is the same
unsolved item the SES path had.

## At-most-once, deliberately

The gap between "we called SES" and "we recorded that we called SES" is the only place a
crash can produce a double send. Two disciplines close it, and the second is a choice
worth defending:

1. The outbox row is moved to `claimed` **in its own committed transaction** before the
   network call. A worker that dies mid-send leaves a `claimed` row, not a `pending` one.
2. **A stale `claimed` row is never automatically retried.** It surfaces for a human.

That is at-most-once, and it means a crash at the wrong moment can lose a send. That is
the correct direction to fail. `010`'s own comment hoped the provider would deduplicate on
the idempotency key; SES does not offer that, so the guarantee has to come from here
instead of from a hope about the provider. A missed pitch costs nothing. A second pitch to
a curator who already got one costs the label its reputation with that curator.

## No silent no-op adapter

There is no `NullMailer` that logs and returns success. An unconfigured deployment raises
`MailNotConfigured`, and `sender.py` refuses to claim the outbox at all. A send that
quietly did not happen, recorded as `sent`, is the worst failure this table can have: the
thread advances, the lesson loop learns from an outcome that never occurred, and nobody
finds out until a curator says they never heard from you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from spindle import settings as settings_mod


class MailNotConfigured(RuntimeError):
    """No verified sender, reply-to, or postal address. Nothing may be sent."""


class MailRefused(RuntimeError):
    """The provider rejected the message. Carries whether retrying could ever work.

    `permanent` is the difference between "SES is throttling us" and "that address does
    not exist". The first goes back to `pending` with a backoff; the second must not,
    because retrying a hard bounce is how a sender's reputation is destroyed.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class Sent:
    """What the provider said. `provider_message_id` goes on the `message` row."""

    provider_message_id: str
    cost_usd: str


class Mailer(Protocol):
    def send(self, *, to: str, subject: str, body: str,
             idempotency_key: str, reply_to: str = "") -> Sent: ...


#: Characters SES permits in a message tag value: ASCII letters, digits, underscore and
#: hyphen, and nothing else.
_TAG_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _tag_value(key: str) -> str:
    """An idempotency key, reduced to what SES will accept as a tag value.

    ## The bug this exists to fix

    Sending the key unmodified worked for as long as every key was hex. `otp.py` then
    introduced keys shaped `otp:<hex>`, and the colon is not in SES's permitted set, so
    **every sign-in code failed to send** with:

        InvalidParameterValue: Invalid tag value <otp:3f8d0955a…>

    Measured against the live deployment on 2026-08-15. `InvalidParameterValue` is in
    SES's never-retry list, so the send was correctly not retried — which meant the whole
    sign-in funnel refused, loudly and permanently, for every address. The mail layer
    was doing exactly the right thing with a message it should never have been asked
    to send.

    Substituting rather than stripping, because the tag is a forensic aid: `otp_3f8d…`
    and `outreach_3f8d…` stay distinguishable, where stripping the separator would run
    the prefix into the digest and make two different keys look alike.
    """
    return _TAG_SAFE.sub("_", key)[:256]


#: Resend answers these when the request itself is wrong — a bad key, an unverified From
#: domain, a malformed address, an idempotency key reused for a different message.
#: Retrying cannot fix any of them. 429 and 5xx are transient and go back to `pending`.
_PERMANENT_STATUS = frozenset({400, 401, 403, 404, 405, 409, 422})

RESEND_URL = "https://api.resend.com/emails"

#: On 429, wait `retry-after` seconds (at most 2, 1 if absent) and try again, up to
#: twice more, with the same Idempotency-Key — so a retry Resend already took is a no-op.
_RETRIES = 2
_MAX_WAIT_S = 2.0


class ResendMailer:
    """Resend's HTTP API, via `urllib`.

    The standard library rather than the `resend` package, for the reason `SMTPMailer`
    uses `smtplib`: one POST, and a bundle whose design is to stay small.

    The `idempotency_key` goes in Resend's `Idempotency-Key` header, which deduplicates
    within 24 hours — so a retried call that Resend had in fact accepted is not a second
    message. That narrows the double-send window this module's header describes, but the
    guarantee is still the claim discipline: Resend's window is a day, and ours is forever.

    A `Message-ID` is minted here and sent as a header, and that — not Resend's own email
    id — is what `Sent.provider_message_id` carries. A curator's client quotes the
    Message-ID back in `In-Reply-To`, and `inbound`'s second rung matches on exactly that;
    Resend's id never appears in the mail at all.
    """

    def __init__(self, *, api_key: str, sender: str, reply_to: str,
                 urlopen: Any = None, sleep: Any = None) -> None:
        self.sender = sender
        self.reply_to = reply_to
        # Never placed in a message or a repr — see SMTPMailer's password.
        self._api_key = api_key
        self._urlopen = urlopen
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"<ResendMailer sender={self.sender!r}>"

    def send(self, *, to: str, subject: str, body: str,
             idempotency_key: str, reply_to: str = "") -> Sent:
        """`reply_to` overrides the configured address, per message.

        The platform sends on behalf of every free tenant from one identity, so the
        routing has to travel in the address itself as `spindle+<reply_token>@…`, and
        that address differs per send (`040_mailbox.sql`). Defaulting to the configured
        value keeps `otp.py`'s sign-in codes — which have no thread — on the plain
        mailbox without every caller having an opinion.
        """
        import json                                   # noqa: PLC0415 — stdlib, lazy
        import time                                   # noqa: PLC0415
        import urllib.error                           # noqa: PLC0415
        import urllib.request                         # noqa: PLC0415
        from email.utils import make_msgid            # noqa: PLC0415

        domain = self.sender.rpartition("@")[2].rstrip(">") or None
        message_id = make_msgid(domain=domain)
        payload = json.dumps({
            "from": self.sender,
            "to": [to],
            "subject": subject,
            "text": body,
            "reply_to": reply_to or self.reply_to,
            "headers": {
                "Message-ID": message_id,
                # The same forensic aid the SMTP and Gmail paths carry.
                "X-Spindle-Idempotency-Key": _tag_value(idempotency_key),
            },
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Resend sits behind a firewall that turns away urllib's own user agent.
            "User-Agent": "spindle/1.0",
            "Idempotency-Key": _tag_value(idempotency_key),
        }
        urlopen = self._urlopen or urllib.request.urlopen
        sleep = self._sleep or time.sleep

        for attempt in range(_RETRIES + 1):
            request = urllib.request.Request(RESEND_URL, data=payload,
                                             headers=headers, method="POST")
            try:
                with urlopen(request, timeout=10) as answer:
                    answer.read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < _RETRIES:
                    sleep(_retry_after(exc))
                    continue
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise MailRefused(f"Resend refused: {exc.code}: {detail}",
                                  permanent=exc.code in _PERMANENT_STATUS) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise MailRefused(f"Resend unreachable ({type(exc).__name__})") from None

        return Sent(provider_message_id=message_id.strip("<>"), cost_usd="0.0001")


def _retry_after(exc: Any) -> float:
    raw = (exc.headers.get("retry-after") if exc.headers else None) or ""
    try:
        seconds = float(raw)
    except ValueError:
        return 1.0
    return min(seconds, _MAX_WAIT_S) if seconds >= 0 else 1.0


def load() -> Mailer:
    """The configured transport, or a refusal. Never a no-op.

    **This checks the transport and not the lawfulness of any particular message**, and
    the distinction is `sender.py`'s, not a new one: that module already splits its two
    guards and says why in full. The short version, because the split is easy to
    misread as a weakening:

      * A **verified sender** and a **reply-to** are what it takes to hand SES a message
        at all. Without the first SES rejects the call; without the second the message is
        one nobody can answer. Those are properties of the transport, so they are checked
        here.
      * A **postal address** is what CAN-SPAM §7704(a)(5) requires in every *commercial*
        message. That is a property of the message, so it is checked where a commercial
        message is composed — `sender.py` refuses to claim the outbox without it, before
        anything is claimed, and does so independently of this function precisely because
        an injected `mailer=` skips the loader entirely. Nothing about that changed.

    Until 2026-08-15 this function required all three, which made the *only* transactional
    message in the system — the sign-in code from `otp.py` — unsendable on a deployment
    with no postal address to give. Since an emailed code is now the only credential, that
    made a lawful, well-configured deployment one nobody could enter, and the only way out
    was to invent a postal address to satisfy a check. Inventing one is exactly the untruth
    this codebase refuses elsewhere, and it would then be printed in the footer of every
    commercial message later enabled.

    `settings.mail_configured` still means all three and is what gates outreach.
    """
    config = settings_mod.load()
    if not config.transactional_mail_configured:
        raise MailNotConfigured(
            "PLATFORM_MAIL_SENDER and PLATFORM_MAIL_REPLY_TO must both be set before "
            "anything can be sent. The sender is not a component that degrades — it "
            "either sends a replyable message from a verified identity, or it does not "
            "run. (Commercial sends additionally need PLATFORM_MAIL_POSTAL_ADDRESS; "
            "sender.py enforces that where the message is composed.)")
    if not config.resend_api_key:
        raise MailNotConfigured(
            "RESEND_API_KEY is not set, so the platform has no transport. In a "
            "deployment it comes from SSM /resend/api-key via Terraform.")
    return ResendMailer(api_key=config.resend_api_key, sender=config.mail_sender,
                        reply_to=config.mail_reply_to)


def compliant_body(body: str, *, postal_address: str, unsubscribe: str) -> str:
    """The message as it will actually be sent, with the footer the law requires.

    CAN-SPAM §7704(a)(5) requires a valid physical postal address in every commercial
    message, and §7704(a)(3) a working opt-out. Both are appended here rather than left to
    whoever composed the draft, because a compliance rule that depends on an author
    remembering it is a rule that will be broken on the day somebody is in a hurry.

    The opt-out line names `contact_route`'s terminal state on purpose: replying STOP is
    not a courtesy, it sets `state = 'opted_out'`, which migration 018 made impossible for
    any discovery stage to overwrite.
    """
    return (
        f"{body.rstrip()}\n\n"
        f"—\n"
        f"{postal_address}\n"
        f"Reply STOP and we will not contact you again from this system.\n"
        f"{unsubscribe}".rstrip() + "\n"
    )


# --------------------------------------------------------------- the tenant's own

class SMTPMailer:
    """Plain SMTP, for every provider that is not Google.

    This is the send half of what `mailbox.imap` reads, and the two are configured from
    one `mail_account` row. That pairing is deliberate: a tenant who can read their
    mailbox but not send from it, or the reverse, has a half-connected account that will
    fail on whichever half nobody tested.

    ## Two rules that are not configurable

    **TLS is always started.** The credential on this connection is the tenant's actual
    mailbox password — not a scoped token, not an API key — and a config flag that could
    turn it off is a flag somebody will turn off to make a connection work. There is no
    parameter for it here for that reason.

    **An authentication failure is permanent.** `mailbox/__init__.py`'s header has the
    argument in full: a provider asked repeatedly for a password it has already refused
    will lock the mailbox at its end, and the tenant then cannot fix it from their side
    either. So a rejected login raises with `permanent=True`, which stops `sender.py`
    retrying it, and the account is marked failed for a person to act on.
    """

    def __init__(self, *, sender: str, host: str, port: int, password: str,
                 reply_to: str = "") -> None:
        self.sender = sender
        self.host = host
        self.port = port
        self.reply_to = reply_to or sender
        # Named with a leading underscore and never placed in a message or a repr. The
        # realistic leak for this value is a traceback, which is why `send` below builds
        # its own error strings rather than passing the provider's through.
        self._password = password

    def __repr__(self) -> str:
        return f"<SMTPMailer sender={self.sender!r} host={self.host!r}>"

    def send(self, *, to: str, subject: str, body: str,
             idempotency_key: str, reply_to: str = "") -> Sent:
        import smtplib                                # noqa: PLC0415 — stdlib, lazy
        from email.message import EmailMessage        # noqa: PLC0415

        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message["Reply-To"] = reply_to or self.reply_to
        # The same forensic aid SES gets as a message tag. It is not an idempotency
        # guarantee — SMTP has no such parameter either — and `sender.py`'s claim
        # discipline remains the actual guarantee. See `ResendMailer`'s docstring.
        message["X-Spindle-Idempotency-Key"] = _tag_value(idempotency_key)
        message.set_content(body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.starttls()
                server.login(self.sender, self._password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailRefused(
                f"{self.host} rejected the stored credential for {self.sender}. "
                f"Reconnect the mailbox with a current password — providers that use "
                f"app passwords will refuse one that has been revoked. (SMTP "
                f"{exc.smtp_code})", permanent=True) from None
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailRefused(f"{self.host} refused the recipient {to}",
                              permanent=True) from None
        except smtplib.SMTPException as exc:
            # Everything else — a dropped connection, a greylisting deferral, a server
            # too busy — is transient by default. Wrong in the safe direction: a retried
            # transient costs one more attempt, a non-retried permanent costs a pitch.
            raise MailRefused(f"{self.host} refused the message "
                              f"({type(exc).__name__})") from None

        # SMTP returns no message id, so we mint the identity we will later match a reply
        # against, and put it in the header we sent. Without this the second rung of
        # `inbound`'s ladder — matching `In-Reply-To` against `provider_message_id` —
        # would have nothing to compare with for every non-Gmail tenant.
        return Sent(provider_message_id=str(message["Message-ID"] or "").strip("<>"),
                    cost_usd="0")


class GmailMailer:
    """Gmail's API, under the same OAuth grant `mailbox.gmail` polls with.

    One credential for both directions rather than two, because they are the same grant:
    a tenant who authorises us to read their mailbox and send as them does it once, and a
    design needing two consents would be asking twice for what Google issues as one.
    """

    def __init__(self, *, sender: str, refresh_token: str, client: Any = None,
                 reply_to: str = "") -> None:
        self.sender = sender
        self.reply_to = reply_to or sender
        self._client = client
        self._refresh_token = refresh_token

    def __repr__(self) -> str:
        return f"<GmailMailer sender={self.sender!r}>"

    def send(self, *, to: str, subject: str, body: str,
             idempotency_key: str, reply_to: str = "") -> Sent:
        import base64                                 # noqa: PLC0415
        from email.message import EmailMessage        # noqa: PLC0415

        from spindle.mailbox import gmail             # noqa: PLC0415 — avoids a cycle

        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message["Reply-To"] = reply_to or self.reply_to
        message["X-Spindle-Idempotency-Key"] = _tag_value(idempotency_key)
        message.set_content(body)

        client = self._client or gmail.RestClient(self._refresh_token)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = client.send_raw(raw)
        # Gmail's own id, not the RFC Message-ID. `inbound.message_id_candidates` handles
        # both forms, which is the same accommodation SES needed.
        return Sent(provider_message_id=str(sent.get("id", "")), cost_usd="0")
