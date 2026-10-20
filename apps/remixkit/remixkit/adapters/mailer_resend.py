"""Resend over its HTTP API.

One POST to `https://api.resend.com/emails` with a Bearer key. `urllib` from the
standard library rather than the `resend` package: this sends one short message, and a
dependency in the API container to do it would be the wrong trade.

The sending domain has to be verified in Resend, with its SPF/DKIM published, or the API
refuses the message (403) — and a sign-in code that never arrives looks exactly like a
broken login. `RK_MAIL_FROM` says which address.

A connection per send, as before: fine at sign-in volume, and nothing is held open
across a Lambda freeze.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from email.utils import formataddr
from typing import Callable

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"

# On 429, wait `retry-after` seconds (at most 2, 1 if absent) and try again, up to
# twice more. A sign-in form is interactive; it should not hang longer than that.
_RETRIES = 2
_MAX_WAIT_S = 2.0


def _retry_after(err: urllib.error.HTTPError) -> float:
    raw = (err.headers.get("retry-after") if err.headers else None) or ""
    try:
        seconds = float(raw)
    except ValueError:
        return 1.0
    return min(seconds, _MAX_WAIT_S) if seconds >= 0 else 1.0


class ResendMailer:
    name = "resend"

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        sender_name: str = "",
        timeout_s: float = 15.0,
        urlopen: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise RuntimeError("mail_backend=resend requires RK_RESEND_API_KEY")
        if not sender:
            raise RuntimeError("mail_backend=resend requires RK_MAIL_FROM")
        self._api_key = api_key
        self._from = formataddr((sender_name or None, sender))
        self._timeout_s = timeout_s
        self._urlopen = urlopen
        self._sleep = sleep

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> bool:
        payload = {"from": self._from, "to": [to], "subject": subject, "text": text}
        if html:
            payload["html"] = html
        body = json.dumps(payload).encode("utf-8")

        for attempt in range(_RETRIES + 1):
            request = urllib.request.Request(
                RESEND_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    # Resend sits behind a firewall that turns away urllib's own user
                    # agent as a bot.
                    "User-Agent": "remixkit/1.0",
                },
                method="POST",
            )
            try:
                with self._urlopen(request, timeout=self._timeout_s) as answer:
                    answer.read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < _RETRIES:
                    self._sleep(_retry_after(exc))
                    continue
                detail = exc.read().decode("utf-8", "replace")[:300]
                # Not fatal, and deliberately not re-raised: a refused send is an
                # outcome of a sign-in attempt. The caller reports "we could not send
                # it" rather than a 500, and the code is still in the log.
                log.error("Resend send to %s failed: %s %s", to, exc.code, detail)
                return False
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log.error("Resend send to %s failed: %s", to, exc)
                return False

        # Recipient only. The subject line carries the sign-in code — "088814 is your
        # RemixKit sign-in code" — so logging it would put every live code into
        # CloudWatch, where it outlives its ten-minute window and is readable by anyone
        # with log access. The console fallback in `services.accounts` logs codes on
        # purpose and only when nothing was sent; this path must not.
        log.info("sent mail to %s via Resend", to)
        return True
