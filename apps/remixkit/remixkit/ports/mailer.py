"""Sending mail — a port, because the console must work with no mail credentials.

There is exactly one thing the app sends today (a sign-in code), and exactly one thing
it needs to know about the outcome: whether the message actually left the building.
That boolean is load-bearing. A sign-in flow that says "check your email" when nothing
was sent is worse than one that refuses, so `send()` returns delivery rather than
raising, and the caller decides what to tell the user.

Two adapters, same shape as every other port here: `mailer_console` (log it, report
`delivered=False`) is the zero-credential path, `mailer_resend` is production.
"""

from __future__ import annotations

from typing import Protocol


class Mailer(Protocol):
    name: str

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> bool:
        """Deliver one message. Returns whether it was actually sent.

        Implementations must not raise for ordinary delivery failure — a bounced SMTP
        session is an expected outcome of a sign-in attempt, not a server fault.
        """
