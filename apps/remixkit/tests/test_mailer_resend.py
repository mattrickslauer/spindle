"""The Resend adapter: what goes over the wire, and that failure is a False, not a raise."""

from __future__ import annotations

import email.message
import io
import json
import logging
import urllib.error

import pytest

from remixkit import deps
from remixkit.adapters.mailer_resend import RESEND_URL, ResendMailer
from remixkit.settings import Settings


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: bytes = b"{}", retry_after: str | None = None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return urllib.error.HTTPError(RESEND_URL, code, "x", headers, io.BytesIO(body))


class FakeOpen:
    """Stands in for urllib.request.urlopen: replays a script of answers/errors."""

    def __init__(self, *script):
        self.script = list(script) or [b'{"id":"em_1"}']
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Answer(step)


def mailer(opener, sleeps=None):
    return ResendMailer(api_key="re_test", sender="rtp@mattrickslauer.com",
                        sender_name="Respect the Funk", urlopen=opener,
                        sleep=(sleeps.append if sleeps is not None else lambda _s: None))


def test_it_posts_the_message_to_resend():
    opener = FakeOpen()
    assert mailer(opener).send(to="a@b.c", subject="123456 is your code",
                               text="plain", html="<b>html</b>") is True
    (request,) = opener.requests
    assert request.full_url == RESEND_URL and request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer re_test"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {
        "from": "Respect the Funk <rtp@mattrickslauer.com>", "to": ["a@b.c"],
        "subject": "123456 is your code", "text": "plain", "html": "<b>html</b>",
    }


def test_html_is_optional():
    opener = FakeOpen()
    mailer(opener).send(to="a@b.c", subject="s", text="t")
    assert "html" not in json.loads(opener.requests[0].data)


def test_a_refused_send_is_false_and_never_logs_the_subject(caplog):
    opener = FakeOpen(_http_error(403, b'{"message":"domain not verified"}'))
    with caplog.at_level(logging.INFO):
        assert mailer(opener).send(to="a@b.c", subject="654321 is your code", text="t") is False
    assert "domain not verified" in caplog.text
    assert "654321" not in caplog.text


def test_an_unreachable_api_is_false():
    opener = FakeOpen(urllib.error.URLError("no route"))
    assert mailer(opener).send(to="a@b.c", subject="s", text="t") is False


def test_a_rate_limit_is_retried_after_the_header():
    sleeps: list[float] = []
    opener = FakeOpen(_http_error(429, retry_after="0.5"), b"{}")
    assert mailer(opener, sleeps).send(to="a@b.c", subject="s", text="t") is True
    assert sleeps == [0.5] and len(opener.requests) == 2


def test_rate_limit_retries_are_bounded():
    sleeps: list[float] = []
    opener = FakeOpen(*[_http_error(429, retry_after="60")] * 3)
    assert mailer(opener, sleeps).send(to="a@b.c", subject="s", text="t") is False
    assert sleeps == [2.0, 2.0] and len(opener.requests) == 3


@pytest.mark.parametrize("missing", ["api_key", "sender"])
def test_it_refuses_to_start_without_its_configuration(missing):
    kwargs = {"api_key": "re_test", "sender": "rtp@mattrickslauer.com", missing: ""}
    with pytest.raises(RuntimeError):
        ResendMailer(**kwargs)


def test_the_container_builds_it_from_settings():
    settings = Settings(_env_file=None, mail_backend="resend", resend_api_key="re_test")
    built = deps._build_mailer(settings)
    assert isinstance(built, ResendMailer) and built.name == "resend"
