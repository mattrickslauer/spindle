"""The sign-in routes and the account page, as the console actually serves them.

    python -m unittest discover apps/spindle/web/tests

`test_otp.py` proves the challenge; this proves the two routes that use it, the cookie
they set, and the page they land on. Handlers are called directly rather than through a
client, which is the house style here and the reason `_request` exists.

The properties this file is for:

  * **The order of the two calls in `signin_verify`.** `otp.verify_code` runs before
    `accounts.sign_in`, and if that ever reverses, typing any address into the form
    creates an account for it. That is the whole security property of the flow, and it is
    one line, so it is asserted by behaviour rather than trusted to review.
  * **The session cookie's flags.** httpOnly, secure, lax, ninety days. Four values that
    are individually easy to drop and invisible when dropped.
  * **The account page offers no control for anything unbuilt** — no email change, no
    device list. A box that silently does nothing is worse than a sentence saying why
    there is no box.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import unittest
import uuid

from spindle import accounts, auth, mail, otp, routes

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, body: str,
             idempotency_key: str) -> mail.Sent:
        self.sent.append({"to": to, "subject": subject, "body": body,
                          "idempotency_key": idempotency_key})
        return mail.Sent(provider_message_id="fake", cost_usd="0.0001")


@contextlib.contextmanager
def _settings(**overrides):
    """Run a block with `mail.settings_mod.load()` returning altered settings.

    `Settings` is a frozen dataclass, so `replace` is the only honest way to vary one
    field — constructing one by hand would silently stop tracking new fields.
    """
    original = mail.settings_mod.load
    base = original()
    mail.settings_mod.load = lambda: dataclasses.replace(base, **overrides)
    try:
        yield
    finally:
        mail.settings_mod.load = original


def _request(path: str = "/signin", method: str = "GET"):
    """A real Starlette `Request`, which the template response needs to render."""
    from starlette.requests import Request

    scope = {
        "type": "http", "method": method, "path": path,
        "headers": [(b"host", b"testserver")],
        "query_string": b"", "root_path": "", "scheme": "https",
        "server": ("testserver", 443), "client": ("test", 1),
        "app": None,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class TransactionalMailIsNotCommercialMail(unittest.TestCase):
    """The split that lets a deployment sign people in without a postal address.

    A sign-in code is transactional: the recipient asked for it seconds ago and there is
    nothing to unsubscribe from. CAN-SPAM §7704(a)(5) requires a physical address in every
    *commercial* message, and outreach is where that applies.

    Before 2026-08-15 `mail.load()` demanded all three variables, which made the only
    transactional message in the system unsendable on a deployment with no postal address
    to give — and since an emailed code is now the only credential, that made a lawful,
    well-configured deployment one nobody could enter. The only escape was to invent an
    address to satisfy a check, which would then be printed in the footer of any
    commercial message later enabled.

    These tests hold both halves: the narrow gate is genuinely narrower, and the
    commercial gate did not move.
    """

    def test_a_sender_and_reply_to_are_enough_to_send_a_code(self):
        with _settings(mail_sender="a@b.com", mail_reply_to="a@b.com",
                       mail_postal_address="", resend_api_key="re_test"):
            self.assertTrue(mail.settings_mod.load().transactional_mail_configured)
            # Builds a real ResendMailer rather than raising. No network until `send`.
            self.assertIsInstance(mail.load(), mail.ResendMailer)

    def test_no_resend_key_is_a_refusal_not_a_silent_no_op(self):
        with _settings(mail_sender="a@b.com", mail_reply_to="a@b.com",
                       resend_api_key=""):
            with self.assertRaises(mail.MailNotConfigured) as caught:
                mail.load()
        self.assertIn("RESEND_API_KEY", str(caught.exception))

    def test_a_missing_postal_address_still_blocks_commercial_mail(self):
        """The half that must not move. `changefeed.py` attaches no outbox sender when
        this is false, and `sender.py` refuses to claim the outbox."""
        with _settings(mail_sender="a@b.com", mail_reply_to="a@b.com",
                       mail_postal_address=""):
            self.assertFalse(mail.settings_mod.load().mail_configured)

    def test_all_three_still_means_commercial_mail_is_configured(self):
        with _settings(mail_sender="a@b.com", mail_reply_to="a@b.com",
                       mail_postal_address="1 Example St"):
            settings = mail.settings_mod.load()
            self.assertTrue(settings.transactional_mail_configured)
            self.assertTrue(settings.mail_configured)

    def test_a_missing_sender_blocks_both(self):
        """SES rejects a send from an unverified identity, so this is the transport
        failing, not a policy choice."""
        with _settings(mail_sender="", mail_reply_to="a@b.com",
                       mail_postal_address="1 Example St"):
            settings = mail.settings_mod.load()
            self.assertFalse(settings.transactional_mail_configured)
            self.assertFalse(settings.mail_configured)
            with self.assertRaises(mail.MailNotConfigured):
                mail.load()

    def test_a_missing_reply_to_blocks_both(self):
        with _settings(mail_sender="a@b.com", mail_reply_to="",
                       mail_postal_address="1 Example St"):
            self.assertFalse(mail.settings_mod.load().transactional_mail_configured)
            with self.assertRaises(mail.MailNotConfigured):
                mail.load()

    def test_the_refusal_does_not_demand_a_postal_address(self):
        """The message a locked-out operator reads. Naming a variable that is not
        required would send them looking for a value they do not have and do not need."""
        with _settings(mail_sender="", mail_reply_to=""):
            with self.assertRaises(mail.MailNotConfigured) as caught:
                mail.load()
        message = str(caught.exception)
        self.assertIn("PLATFORM_MAIL_SENDER", message)
        self.assertIn("PLATFORM_MAIL_REPLY_TO", message)
        # Mentioned only as the commercial-send caveat, never as something to set to
        # get a code out.
        self.assertIn("Commercial sends", message)

    def test_the_sign_in_code_carries_no_can_spam_footer(self):
        """The reason the postal address is not required, asserted rather than argued.

        If a code ever went out through `compliant_body`, it would invite somebody to
        reply STOP to a login email — which sets `contact_route.state = 'opted_out'` on a
        party who may be in an active campaign.

        Checked against the parsed module rather than its text: `otp.py`'s docstring
        names `compliant_body` at length, explaining why it does *not* call it, so a
        substring search finds the argument and reports it as the violation.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path(otp.__file__).read_text(encoding="utf-8"))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else
            getattr(node.func, "id", "")
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        self.assertNotIn("compliant_body", called)
        # And the body it does send carries no opt-out line.
        self.assertNotIn("STOP", otp._BODY)
        self.assertNotIn("unsubscribe", otp._BODY.lower())


class TheSessionCookie(unittest.TestCase):
    """No database: these are properties of `_set_session` and one constant."""

    def test_the_session_lasts_ninety_days(self):
        """Sign-in persists — the thing that was asked for. Asserted as a number next to
        the reason, rather than left as a constant nobody rechecks."""
        self.assertEqual(60 * 60 * 24 * 90, routes.SESSION_SECONDS)

    def test_every_flag_the_cookie_has_always_had_is_still_set(self):
        """httpOnly so console JavaScript cannot read it and an XSS cannot exfiltrate it,
        secure so it never crosses plain HTTP, lax so a link from an email arrives signed
        in while a cross-site POST does not.

        Read off the rendered `set-cookie` header rather than off the call, because the
        header is what a browser acts on and it is what a wrong keyword would silently
        fail to produce."""
        from fastapi.responses import RedirectResponse

        response = RedirectResponse("/", status_code=303)
        routes._set_session(response, "a-token")
        header = response.headers["set-cookie"].lower()

        self.assertIn(f"{auth.COOKIE_NAME.lower()}=a-token", header)
        self.assertIn("httponly", header)
        self.assertIn("secure", header)
        self.assertIn("samesite=lax", header)
        self.assertIn(f"max-age={routes.SESSION_SECONDS}", header)

    def test_signing_out_clears_the_cookie(self):
        header = routes.signout().headers["set-cookie"].lower()
        self.assertIn(auth.COOKIE_NAME.lower(), header)
        # Starlette expresses a deletion as an empty value with an expiry in the past.
        self.assertIn('=""', header.replace("=;", '="";'))


@unittest.skipUnless(HAVE_DB, "needs DATABASE_URL")
class TheSignInFlow(unittest.TestCase):

    def setUp(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True,
                                    row_factory=dict_row)
        self.mailer = FakeMailer()
        self.addresses: list[str] = []
        self.tenants: list[str] = []

        # `routes._conn` opens the module-level pooled connection; point it at ours so
        # the handler and the assertions see the same rows, and `mail.load` at a fake so
        # nothing can reach SES from a test.
        self._real_conn, self._real_load = routes._conn, mail.load
        routes._conn = lambda: self.conn
        mail.load = lambda: self.mailer

    def tearDown(self) -> None:
        routes._conn, mail.load = self._real_conn, self._real_load
        with self.conn.cursor() as cur:
            for email in self.addresses:
                cur.execute("DELETE FROM otp_challenge WHERE email = %s", (email,))
            for tenant_id in self.tenants:
                cur.execute("DELETE FROM tenant WHERE id = %s", (tenant_id,))
        self.conn.close()

    def _address(self) -> str:
        email = f"signin-{uuid.uuid4().hex[:12]}@example.com"
        self.addresses.append(email)
        return email

    def _code_from_mail(self) -> str:
        import re
        found = re.findall(r"\b\d{6}\b", self.mailer.sent[-1]["body"])
        self.assertEqual(1, len(found))
        return found[0]

    def _remember_tenant(self, email: str) -> None:
        account = accounts.account_by_email(self.conn, email)
        if account is not None:
            self.tenants.append(str(account["tenant_id"]))

    # ------------------------------------------------------------- step one

    def test_requesting_a_code_mails_it_and_moves_to_step_two(self):
        email = self._address()
        response = routes.signin_request_code(
            _request("/signin/code", "POST"), auth.ANONYMOUS, email=email)
        self.assertEqual(200, response.status_code)
        self.assertEqual(email, self.mailer.sent[-1]["to"])
        self.assertEqual("code", response.context["step"])

    def test_an_unconfigured_mailer_names_the_variables_rather_than_pretending(self):
        """Mail is the only way in now, so this is the refusal that matters most. It must
        say which variables are unset — there is no reveal path to fall back to, and a
        generic 'could not send' would send the operator looking in the wrong place."""
        mail.load = self._real_load          # the real loader, against empty settings
        with _settings(mail_sender="", mail_reply_to=""):
            email = self._address()
            response = routes.signin_request_code(
                _request("/signin/code", "POST"), auth.ANONYMOUS, email=email)

        self.assertEqual(503, response.status_code)
        for name in ("PLATFORM_MAIL_SENDER", "PLATFORM_MAIL_REPLY_TO"):
            self.assertIn(name, response.context["error"])

    def test_no_code_is_ever_shown_on_the_page(self):
        """The no-reveal guarantee, asserted rather than described. If a `dev_code` path
        is ever added, this is what should fail."""
        email = self._address()
        response = routes.signin_request_code(
            _request("/signin/code", "POST"), auth.ANONYMOUS, email=email)
        rendered = " ".join(str(v) for v in response.context.values())
        self.assertNotIn(self._code_from_mail(), rendered)

    # ------------------------------------------------------------- step two

    def test_verifying_signs_in_and_sets_the_session(self):
        email = self._address()
        routes.signin_request_code(_request("/signin/code", "POST"),
                                   auth.ANONYMOUS, email=email)
        response = routes.signin_verify(
            _request("/signin/verify", "POST"), auth.ANONYMOUS,
            email=email, code=self._code_from_mail())
        self._remember_tenant(email)

        self.assertEqual(303, response.status_code)
        self.assertEqual("/", response.headers["location"])
        self.assertIn("httponly", response.headers["set-cookie"].lower())

    def test_a_first_sign_in_creates_the_tenant_and_a_second_reuses_it(self):
        email = self._address()
        for _ in range(2):
            routes.signin_request_code(_request("/signin/code", "POST"),
                                       auth.ANONYMOUS, email=email)
            routes.signin_verify(_request("/signin/verify", "POST"), auth.ANONYMOUS,
                                 email=email, code=self._code_from_mail())
            # The resend floor is per-address; age the row so the second request is
            # allowed rather than sleeping through thirty seconds.
            with self.conn.cursor() as cur:
                cur.execute(
                    """UPDATE otp_challenge SET sent_at = sent_at - INTERVAL '1 minute'
                        WHERE email = %s""", (email,))
        self._remember_tenant(email)

        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM account WHERE email = %s", (email,))
            self.assertEqual(1, cur.fetchone()["n"])

    def test_a_wrong_code_signs_nobody_in_and_creates_nothing(self):
        """The property that makes the order of the two calls in `signin_verify` matter.

        If `accounts.sign_in` ever ran before `otp.verify_code`, typing any address into
        the form would create an account for it — no code required. This asserts the
        negative directly: a failed verification leaves no account behind.
        """
        email = self._address()
        routes.signin_request_code(_request("/signin/code", "POST"),
                                   auth.ANONYMOUS, email=email)
        response = routes.signin_verify(
            _request("/signin/verify", "POST"), auth.ANONYMOUS,
            email=email, code="000000")

        self.assertEqual(401, response.status_code)
        self.assertNotIn("set-cookie", response.headers)
        self.assertIsNone(accounts.account_by_email(self.conn, email))

    def test_verifying_with_no_code_requested_creates_nothing(self):
        """The same property from the other side: an address that never asked for a code
        cannot be turned into an account by posting straight to the verify endpoint."""
        email = self._address()
        response = routes.signin_verify(
            _request("/signin/verify", "POST"), auth.ANONYMOUS,
            email=email, code="123456")

        self.assertEqual(401, response.status_code)
        self.assertIsNone(accounts.account_by_email(self.conn, email))

    # -------------------------------------------------------- the account page

    def test_the_account_page_renders_for_a_signed_in_tenant(self):
        email = self._address()
        account, _ = accounts.sign_in(self.conn, email)
        self.tenants.append(str(account["tenant_id"]))
        principal = auth.Principal(
            tenant_id=str(account["tenant_id"]), subject=email, authenticated=True,
            plan=str(account["plan"]), tenant_slug=str(account["tenant_slug"]))

        response = routes.account_page(_request("/account"), principal)

        self.assertEqual(200, response.status_code)
        self.assertEqual("account", response.context["here"])
        self.assertEqual(email, response.context["account"]["email"])
        self.assertEqual("free", response.context["tier"].key)

    def test_rotating_the_token_signs_this_browser_out_too(self):
        """Rotation invalidates the cookie the person is holding, so the response has to
        drop it. Leaving it would bounce them to sign-in from every page with no
        explanation."""
        email = self._address()
        account, first_token = accounts.sign_in(self.conn, email)
        self.tenants.append(str(account["tenant_id"]))
        principal = auth.Principal(
            tenant_id=str(account["tenant_id"]), subject=email, authenticated=True,
            plan=str(account["plan"]), tenant_slug=str(account["tenant_slug"]))

        response = routes.account_rotate(principal)

        self.assertEqual(303, response.status_code)
        self.assertEqual("/signin", response.headers["location"])
        self.assertIn("set-cookie", response.headers)
        self.assertIsNone(accounts.account_for_token(self.conn, first_token))


class TheAccountPageOffersNothingUnbuilt(unittest.TestCase):
    """Read off the template source. A control for a flow that does not exist is worse
    than its absence, because a person will use it and believe it worked."""

    def _template(self) -> str:
        from pathlib import Path

        return Path(routes.__file__).resolve().parent.joinpath(
            "templates", "console", "account.html").read_text(encoding="utf-8")

    def test_there_is_no_email_field(self):
        """Changing an address means proving the new one before the old one stops
        working, which is not built. The page says so in words instead."""
        source = self._template()
        self.assertNotIn('name="email"', source)
        self.assertIn("is not built", source)

    def test_sign_out_everywhere_admits_it_is_the_only_revocation(self):
        """One session per account is migration 033's accepted cost. The page states it
        rather than implying a device list."""
        self.assertIn("only revocation", self._template())


if __name__ == "__main__":
    unittest.main()
