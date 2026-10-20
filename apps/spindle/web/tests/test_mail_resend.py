"""The platform transport: what reaches Resend, and how its answers map onto MailRefused.

No network. `urlopen` is injected, the same seam `GmailMailer` uses for its client.
"""

from __future__ import annotations

import email.message
import io
import json
import unittest
import urllib.error

from spindle import mail


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=b"{}", retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return urllib.error.HTTPError(mail.RESEND_URL, code, "x", headers, io.BytesIO(body))


class FakeOpen:
    def __init__(self, *script):
        self.script = list(script) or [b'{"id":"resend-id"}']
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Answer(step)


def _mailer(opener, sleeps=None):
    return mail.ResendMailer(
        api_key="re_test", sender="Spindle <spindle@mattrickslauer.com>",
        reply_to="spindle@mattrickslauer.com", urlopen=opener,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))


def _send(mailer, **kw):
    args = {"to": "curator@example.com", "subject": "123456 is your code",
            "body": "plain body", "idempotency_key": "otp-3f8d"}
    args.update(kw)
    return mailer.send(**args)


class WhatGoesOverTheWire(unittest.TestCase):
    def test_the_request(self):
        opener = FakeOpen()
        sent = _send(_mailer(opener))
        (request,) = opener.requests
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer re_test")
        self.assertEqual(request.get_header("Idempotency-key"), "otp-3f8d")
        payload = json.loads(request.data)
        self.assertEqual(payload["from"], "Spindle <spindle@mattrickslauer.com>")
        self.assertEqual(payload["to"], ["curator@example.com"])
        self.assertEqual(payload["subject"], "123456 is your code")
        self.assertEqual(payload["text"], "plain body")
        self.assertEqual(payload["reply_to"], "spindle@mattrickslauer.com")
        self.assertEqual(payload["headers"]["X-Spindle-Idempotency-Key"], "otp-3f8d")
        # The id we record is the Message-ID a reply will quote, not Resend's own id.
        message_id = payload["headers"]["Message-ID"]
        self.assertTrue(message_id.endswith("@mattrickslauer.com>"))
        self.assertEqual(sent.provider_message_id, message_id.strip("<>"))

    def test_a_per_message_reply_to_wins(self):
        opener = FakeOpen()
        _send(_mailer(opener), reply_to="spindle+tok@mattrickslauer.com")
        self.assertEqual(json.loads(opener.requests[0].data)["reply_to"],
                         "spindle+tok@mattrickslauer.com")

    def test_the_key_is_not_in_the_repr(self):
        self.assertNotIn("re_test", repr(_mailer(FakeOpen())))


class Refusals(unittest.TestCase):
    def test_a_bad_request_is_permanent(self):
        for code in (400, 401, 403, 422):
            with self.subTest(code=code), self.assertRaises(mail.MailRefused) as caught:
                _send(_mailer(FakeOpen(_http_error(code, b'{"message":"nope"}'))))
            self.assertTrue(caught.exception.permanent)
            self.assertIn("nope", str(caught.exception))

    def test_a_server_error_is_transient(self):
        with self.assertRaises(mail.MailRefused) as caught:
            _send(_mailer(FakeOpen(_http_error(503))))
        self.assertFalse(caught.exception.permanent)

    def test_unreachable_is_transient(self):
        with self.assertRaises(mail.MailRefused) as caught:
            _send(_mailer(FakeOpen(urllib.error.URLError("no route"))))
        self.assertFalse(caught.exception.permanent)

    def test_a_rate_limit_is_retried_with_the_same_key(self):
        sleeps = []
        opener = FakeOpen(_http_error(429, retry_after="0.5"), b"{}")
        _send(_mailer(opener, sleeps))
        self.assertEqual(sleeps, [0.5])
        self.assertEqual([r.get_header("Idempotency-key") for r in opener.requests],
                         ["otp-3f8d", "otp-3f8d"])

    def test_rate_limit_retries_are_bounded_and_then_transient(self):
        sleeps = []
        opener = FakeOpen(*[_http_error(429, retry_after="60")] * 3)
        with self.assertRaises(mail.MailRefused) as caught:
            _send(_mailer(opener, sleeps))
        self.assertFalse(caught.exception.permanent)
        self.assertEqual(sleeps, [2.0, 2.0])
        self.assertEqual(len(opener.requests), 3)


if __name__ == "__main__":
    unittest.main()
