"""Email-OTP sign-in, end to end and at the edges.

The service tests drive `AccountService` directly because that is where the decisions
live; the HTTP tests exist to prove the two things that are only true once FastAPI is
involved — that an unauthenticated browser is redirected while an unauthenticated script
gets a 401, and that the console's cookie and the API's bearer token are the same token.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from remixkit import deps
from remixkit.domain.models import Account, OtpChallenge, utcnow
from remixkit.services.accounts import CHALLENGES, AuthRefused, TooManyAttempts, account_id
from remixkit.services.kits import JOB_TYPE
from remixkit.settings import Settings, get_settings

FIRST_USER = "rtp@agfarms.dev"
SECRET = "test-secret-not-the-real-one"


class FakeMailer:
    """A mailer that succeeds and remembers. Standing in for Resend."""

    name = "fake"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> bool:
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})
        return True

    @property
    def last_code(self) -> str:
        match = re.search(r"\b(\d{6})\b", self.sent[-1]["text"])
        assert match, f"no six-digit code in {self.sent[-1]['text']!r}"
        return match.group(1)


@pytest.fixture
def otp_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,  # see conftest._hermetic_env
        env="dev",
        local_storage_dir=tmp_path / "data",
        storage_backend="local",
        generator_backend="mock",
        queue_backend="inline",
        default_tenant_id="test-label",
        auth_backend="otp",
        mail_backend="console",
        allowed_emails=f"{FIRST_USER}, Second@Agfarms.dev ",
        session_secret=SECRET,
    )


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def otp_container(otp_settings, mailer, monkeypatch) -> deps.Container:
    built = deps.Container(otp_settings)
    # Swap the console mailer for one that reports delivery, so the tests exercise the
    # real path (code goes out by email, nothing is echoed back) rather than the
    # laptop fallback. `_reveal_codes` stays on but has nothing to reveal.
    built.mailer = mailer
    built.accounts._mailer = mailer
    built.enqueued = []  # type: ignore[attr-defined]
    built.queue._handlers.clear()
    built.queue.register(JOB_TYPE, lambda payload: built.enqueued.append(payload))  # type: ignore[attr-defined]
    monkeypatch.setattr(deps, "get_container", lambda: built)
    get_settings.cache_clear()
    return built


@pytest.fixture
def otp_client(otp_container, monkeypatch) -> TestClient:
    import remixkit.api.v1 as api_v1
    import remixkit.main as main_mod
    import remixkit.ui.routes as ui_routes

    for module in (api_v1, main_mod, ui_routes):
        monkeypatch.setattr(module, "get_container", lambda: otp_container, raising=False)
    monkeypatch.setattr(deps, "get_container", lambda: otp_container)
    return TestClient(main_mod.create_app())


def sign_in(container, mailer, email: str = FIRST_USER) -> str:
    """The happy path, as a helper — returns the bearer token."""
    container.accounts.request_code(email)
    _, token = container.accounts.verify_code(email, mailer.last_code)
    return token


# ------------------------------------------------------------------ the first user
def test_first_sign_in_creates_the_account(otp_container, mailer):
    accounts = otp_container.accounts

    sent = accounts.request_code(FIRST_USER)
    assert sent.delivered is True
    assert sent.dev_code is None, "a delivered code must never be echoed back"
    assert mailer.sent[-1]["to"] == FIRST_USER
    assert mailer.last_code in mailer.sent[-1]["html"]

    account, token = accounts.verify_code(FIRST_USER, mailer.last_code)
    assert account.email == FIRST_USER
    assert account.tenant_id == "test-label"
    assert account.last_login_at is not None
    assert "kits:approve" in account.scopes
    assert token

    # The account is a durable document, not a session artefact.
    stored = otp_container.repo.get("test-label", "accounts", account_id(FIRST_USER), Account)
    assert stored is not None and stored.email == FIRST_USER


def test_second_sign_in_reuses_the_account(otp_container, mailer):
    accounts = otp_container.accounts
    first, _ = accounts.verify_code(
        FIRST_USER, (accounts.request_code(FIRST_USER), mailer.last_code)[1]
    )
    accounts.request_code(FIRST_USER)
    second, _ = accounts.verify_code(FIRST_USER, mailer.last_code)

    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.last_login_at >= first.last_login_at


def test_email_is_normalised(otp_container, mailer):
    """The allowlist is case-insensitive and the account is keyed by the lowered form."""
    accounts = otp_container.accounts
    sent = accounts.request_code("  SECOND@agfarms.DEV ")
    assert sent.email == "second@agfarms.dev"
    account, _ = accounts.verify_code("Second@Agfarms.dev", mailer.last_code)
    assert account.email == "second@agfarms.dev"


# ------------------------------------------------------------------ refusals
def test_unlisted_address_is_refused_and_sends_nothing(otp_container, mailer):
    with pytest.raises(AuthRefused):
        otp_container.accounts.request_code("stranger@example.com")
    assert mailer.sent == []


def test_malformed_address_is_refused(otp_container):
    with pytest.raises(AuthRefused):
        otp_container.accounts.request_code("not-an-email")


def test_wrong_code_is_refused_and_the_challenge_survives(otp_container, mailer):
    accounts = otp_container.accounts
    accounts.request_code(FIRST_USER)
    real = mailer.last_code
    wrong = "000000" if real != "000000" else "111111"

    with pytest.raises(AuthRefused):
        accounts.verify_code(FIRST_USER, wrong)
    # One bad guess must not burn the code — the user gets to retype it.
    account, _ = accounts.verify_code(FIRST_USER, real)
    assert account.email == FIRST_USER


def test_attempts_are_capped_and_the_challenge_is_destroyed(otp_container, mailer):
    accounts = otp_container.accounts
    accounts.request_code(FIRST_USER)
    real = mailer.last_code
    wrong = "000000" if real != "000000" else "111111"

    for _ in range(otp_container.settings.otp_max_attempts - 1):
        with pytest.raises(AuthRefused):
            accounts.verify_code(FIRST_USER, wrong)
    with pytest.raises(TooManyAttempts):
        accounts.verify_code(FIRST_USER, wrong)

    # Exhausted means gone: the correct code no longer works either.
    with pytest.raises(AuthRefused):
        accounts.verify_code(FIRST_USER, real)


def test_a_code_is_single_use(otp_container, mailer):
    accounts = otp_container.accounts
    accounts.request_code(FIRST_USER)
    code = mailer.last_code
    accounts.verify_code(FIRST_USER, code)
    with pytest.raises(AuthRefused):
        accounts.verify_code(FIRST_USER, code)


def test_expired_code_is_refused(otp_container, mailer):
    accounts = otp_container.accounts
    accounts.request_code(FIRST_USER)
    code = mailer.last_code

    doc_id = account_id(FIRST_USER)
    challenge = otp_container.repo.get("test-label", CHALLENGES, doc_id, OtpChallenge)
    challenge.expires_at = utcnow() - timedelta(seconds=1)
    otp_container.repo.put("test-label", CHALLENGES, doc_id, challenge)

    with pytest.raises(AuthRefused):
        accounts.verify_code(FIRST_USER, code)


def test_resend_is_rate_limited(otp_container):
    accounts = otp_container.accounts
    accounts.request_code(FIRST_USER)
    with pytest.raises(TooManyAttempts):
        accounts.request_code(FIRST_USER)


def test_the_stored_challenge_never_holds_the_code(otp_container, mailer):
    otp_container.accounts.request_code(FIRST_USER)
    challenge = otp_container.repo.get(
        "test-label", CHALLENGES, account_id(FIRST_USER), OtpChallenge
    )
    assert mailer.last_code not in challenge.code_hash
    assert len(challenge.code_hash) == 64  # hex sha256


# ------------------------------------------------------------------ sessions
def test_session_round_trip(otp_container, mailer):
    token = sign_in(otp_container, mailer)
    claims = otp_container.accounts.read_session(token)
    assert claims["sub"] == FIRST_USER
    assert claims["tid"] == "test-label"
    assert "kits:approve" in claims["scopes"]


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda t: t[:-2], id="truncated-signature"),
        pytest.param(lambda t: t.split(".")[0], id="no-signature"),
        pytest.param(lambda t: "garbage", id="not-a-token"),
        pytest.param(lambda t: "", id="empty"),
        pytest.param(lambda t: "x" + t, id="payload-tampered"),
    ],
)
def test_a_broken_token_is_not_a_session(otp_container, mailer, mangle):
    token = sign_in(otp_container, mailer)
    assert otp_container.accounts.read_session(mangle(token)) is None


def test_a_token_signed_with_another_key_is_refused(otp_container, otp_settings, mailer):
    token = sign_in(otp_container, mailer)
    other = deps.Container(otp_settings.model_copy(update={"session_secret": "different"}))
    assert other.accounts.read_session(token) is None


def test_an_expired_token_is_refused(otp_container, mailer):
    otp_container.accounts._session_ttl_s = -1
    token = sign_in(otp_container, mailer)
    assert otp_container.accounts.read_session(token) is None


def test_removal_from_the_allowlist_ends_the_session(otp_container, mailer):
    """A stateless token cannot be revoked — but the allowlist is checked on every read,
    so dropping someone takes effect on their next request rather than at expiry."""
    token = sign_in(otp_container, mailer)
    assert otp_container.accounts.read_session(token) is not None
    otp_container.accounts._allowlist = frozenset()
    assert otp_container.accounts.read_session(token) is None


# ------------------------------------------------------------------ over HTTP
def test_browser_without_a_session_is_sent_to_the_login_page(otp_client):
    response = otp_client.get(
        "/console", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    # No `?next=` — the console root is where sign-in lands anyway.
    assert response.headers["location"] == "/auth/login"


def test_the_intended_destination_survives_the_redirect(otp_client):
    response = otp_client.get(
        "/console/artists/art_123", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert (
        response.headers["location"] == "/auth/login?next=%2Fconsole%2Fartists%2Fart_123"
    )


@pytest.mark.parametrize(
    "path", ["/console", "/console/catalogue", "/console/settings"]
)
def test_every_console_screen_is_behind_the_login(otp_client, path):
    """No route has an auth branch, so this is really a check that each page handler
    took `CurrentPrincipal` — the omission that would silently expose a screen."""
    response = otp_client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


def test_the_verifier_stays_public(otp_client):
    """PRODUCT.md keeps /verify usable without an account. It reads no tenant data —
    `manifest.verify()` runs over uploaded bytes — so this is intent, not an oversight.
    It sits outside `/console` precisely so the URL does not imply a gate."""
    response = otp_client.get("/verify", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 200


def test_the_landing_page_is_public(otp_client):
    """`/` must render for a stranger even with auth on — it is the marketing site, and
    it carries the only link a new operator has into the login."""
    response = otp_client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 200
    assert 'href="/auth/login"' in response.text
    assert "Sign in" in response.text


def test_the_landing_page_offers_the_console_to_someone_signed_in(otp_client, mailer):
    otp_client.post("/ui/auth/request-code", data={"email": FIRST_USER})
    otp_client.post("/ui/auth/verify-code", data={"email": FIRST_USER, "code": mailer.last_code})

    response = otp_client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert 'href="/console"' in response.text
    # A login link here would bounce them straight back to the console.
    assert 'href="/auth/login"' not in response.text


def test_api_without_a_token_is_401_json_not_a_redirect(otp_client):
    response = otp_client.get("/api/v1/artists", follow_redirects=False)
    assert response.status_code == 401
    assert response.json()["detail"]


def test_htmx_gets_a_client_side_redirect(otp_client):
    response = otp_client.post(
        "/ui/artists", data={"name": "Nobody"}, headers={"hx-request": "true"}
    )
    assert response.status_code == 204
    assert response.headers["hx-redirect"] == "/auth/login"


def test_the_login_page_renders_without_a_session(otp_client):
    response = otp_client.get("/auth/login", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_healthz_stays_open(otp_client):
    """Auth must not break the thing a load balancer polls."""
    response = otp_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["auth"] == "otp"


def test_bearer_token_opens_the_api(otp_client, otp_container, mailer):
    token = sign_in(otp_container, mailer)
    response = otp_client.get("/api/v1/artists", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200

    me = otp_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["subject"] == FIRST_USER
    assert me.json()["authenticated"] is True


def test_the_full_console_sign_in_sets_a_cookie_and_admits_the_user(otp_client, mailer):
    step_one = otp_client.post(
        "/ui/auth/request-code", data={"email": FIRST_USER, "next": "/console/verify"}
    )
    assert step_one.status_code == 200
    assert FIRST_USER in step_one.text
    assert mailer.last_code not in step_one.text, "a delivered code must not reach the page"

    step_two = otp_client.post(
        "/ui/auth/verify-code",
        data={"email": FIRST_USER, "code": mailer.last_code, "next": "/console/verify"},
    )
    assert step_two.status_code == 204
    assert step_two.headers["hx-redirect"] == "/console/verify"

    cookie = otp_client.cookies.get("rk_session")
    assert cookie
    # The cookie the browser now holds is a real session for the roster.
    roster = otp_client.get("/console", headers={"accept": "text/html"})
    assert roster.status_code == 200
    assert "Sign out" in roster.text


def test_signing_out_drops_the_cookie(otp_client, otp_container, mailer):
    otp_client.post("/ui/auth/request-code", data={"email": FIRST_USER})
    otp_client.post("/ui/auth/verify-code", data={"email": FIRST_USER, "code": mailer.last_code})
    assert otp_client.cookies.get("rk_session")

    response = otp_client.get("/auth/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"
    assert not otp_client.cookies.get("rk_session")


def test_a_wrong_code_over_http_re_renders_the_form(otp_client, mailer):
    otp_client.post("/ui/auth/request-code", data={"email": FIRST_USER})
    real = mailer.last_code
    response = otp_client.post(
        "/ui/auth/verify-code",
        data={"email": FIRST_USER, "code": "000000" if real != "000000" else "111111"},
    )
    assert response.status_code == 401
    assert "not right" in response.text
    assert "Sign in" in response.text  # the form is still there to retry in


def test_a_refused_address_comes_back_with_the_form(otp_client, mailer):
    """The card must never end up with no form in it — a typo would cost a reload."""
    response = otp_client.post("/ui/auth/request-code", data={"email": "stranger@example.com"})
    assert response.status_code == 401
    assert "not permitted" in response.text
    assert 'name="email"' in response.text
    assert 'value="stranger@example.com"' in response.text
    assert mailer.sent == []


@pytest.mark.parametrize(
    "hostile", ["https://evil.example/", "//evil.example/", "http://evil.example"]
)
def test_next_cannot_be_used_as_an_open_redirect(otp_client, mailer, hostile):
    otp_client.post("/ui/auth/request-code", data={"email": FIRST_USER, "next": hostile})
    response = otp_client.post(
        "/ui/auth/verify-code",
        data={"email": FIRST_USER, "code": mailer.last_code, "next": hostile},
    )
    assert response.headers["hx-redirect"] == "/console"


def test_the_asset_route_is_not_a_way_around_the_login(otp_client):
    """`/files/{key}` reads an arbitrary object key out of the URL. Unauthenticated, it
    would be the one route that hands over bytes without asking who is asking."""
    response = otp_client.get("/files/remixkit/masters/test-label/song.mp3")
    assert response.status_code == 401


def test_documents_are_never_served_as_files(otp_client, otp_container, mailer):
    """Even signed in: the account YAML holds every operator's email address, and no
    page in the console links to it."""
    token = sign_in(otp_container, mailer)
    key = f"remixkit/tenants/test-label/accounts/{account_id(FIRST_USER)}.yaml"
    response = otp_client.get(f"/files/{key}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
    assert FIRST_USER not in response.text


def test_a_tenants_own_media_is_served(otp_client, otp_container, mailer):
    """The regression this fixes. Identity reference frames live under `tenants/` and the
    identity page links to every one of them, so refusing the whole prefix rendered an
    artist with five broken images while the bytes sat on disk.

    Invisible on B2, where frames are presigned and this route is never reached — which is
    how a dev-only path rots without anybody noticing.
    """
    token = sign_in(otp_container, mailer)
    key = "remixkit/tenants/test-label/identities/idn_1/frames/abc.jpeg"
    otp_container.storage.put(key, b"\xff\xd8\xff-jpeg-bytes", content_type="image/jpeg")

    response = otp_client.get(f"/files/{key}", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xff-jpeg-bytes"
    assert response.headers["content-type"] == "image/jpeg"


def test_another_tenants_media_is_not(otp_client, otp_container, mailer):
    """The handler takes an arbitrary key from the URL, so serving a tenant's media means
    checking it is *their* tenant — otherwise a signed-in user reads someone else's
    photographs by typing the path."""
    token = sign_in(otp_container, mailer)
    key = "remixkit/tenants/someone-else/identities/idn_9/frames/xyz.jpeg"
    otp_container.storage.put(key, b"not-yours", content_type="image/jpeg")

    response = otp_client.get(f"/files/{key}", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 404, "another tenant's frames are not this caller's"
    assert b"not-yours" not in response.content


def test_documents_are_still_refused_inside_your_own_tenant(otp_client, otp_container, mailer):
    """Widening this to media must not widen it to the repository's private storage."""
    token = sign_in(otp_container, mailer)
    key = "remixkit/tenants/test-label/artists/art_1.yaml"
    otp_container.storage.put(key, b"id: art_1\n", content_type="application/yaml")

    response = otp_client.get(f"/files/{key}", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 404


def test_the_login_page_bounces_someone_already_signed_in(otp_client, mailer):
    otp_client.post("/ui/auth/request-code", data={"email": FIRST_USER})
    otp_client.post("/ui/auth/verify-code", data={"email": FIRST_USER, "code": mailer.last_code})

    response = otp_client.get(
        "/auth/login", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/console"


# ------------------------------------------------------------------ the default shape
def test_auth_off_by_default_still_admits_everyone(client):
    """The `none` backend is unchanged — this is the regression guard on that promise."""
    assert client.get("/api/v1/artists").status_code == 200
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200
    assert client.get("/console", headers={"accept": "text/html"}).status_code == 200


def test_require_auth_refuses_an_unauthenticated_deployment(otp_settings):
    with pytest.raises(RuntimeError, match="RK_AUTH_BACKEND=none"):
        deps.Container(otp_settings.model_copy(update={"auth_backend": "none", "require_auth": True}))


def test_require_auth_refuses_an_ephemeral_signing_key(otp_settings):
    with pytest.raises(RuntimeError, match="RK_SESSION_SECRET"):
        deps.Container(otp_settings.model_copy(update={"session_secret": "", "require_auth": True}))
