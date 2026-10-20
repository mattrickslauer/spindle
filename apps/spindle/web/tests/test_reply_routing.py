"""The address a reply is asked to come back to, and who is allowed to change it.

`040_mailbox.sql` builds the table and `inbound.py` reads a token out of an arriving
message. This file covers the other end: the token has to *get onto the wire* in the
first place, as a plus-addressed `Reply-To` on the pitch itself, or rung one of the
correlation ladder never fires and every reply falls through to the weaker rungs.

The tier half is here too, because it is the same decision seen from the product side:
whose address a pitch goes out from is what a tenant is paying for when they connect a
mailbox, and the free tier's answer is "the platform's".
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from spindle import inbound, mail, plans, sender

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class ReplyToIsPerMessageAndNotPerMailer(unittest.TestCase):
    """The platform mailer used to hold one `reply_to` for its whole life, which was
    right while every message went to the same mailbox. A per-thread token cannot work
    that way — the address differs per send — so `send` grows the parameter."""

    def _send(self, **kwargs) -> dict:
        import io
        import json

        captured: list = []

        class _Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None):
            captured.append(json.loads(request.data))
            return _Answer(b'{"id":"r-1"}')

        mail.ResendMailer(api_key="re_test", sender="spindle@example.com",
                          reply_to="spindle@example.com", urlopen=urlopen).send(
            to="curator@example.org", subject="s", body="b", idempotency_key="k",
            **kwargs)
        return captured[0]

    def test_a_per_message_reply_to_reaches_resend(self) -> None:
        sent = self._send(reply_to="spindle+tok123@example.com")
        self.assertEqual(sent["reply_to"], "spindle+tok123@example.com")

    def test_omitting_it_falls_back_to_the_configured_address(self) -> None:
        """Transactional mail — the OTP codes — has no thread and wants the plain
        mailbox. It must keep working without passing anything."""
        self.assertEqual(self._send()["reply_to"], "spindle@example.com")


class TheTierDecidesWhoseAddressSends(unittest.TestCase):

    def test_the_free_tier_may_not_connect_a_mailbox(self) -> None:
        """A connected mailbox costs a stored secret per tenant, and a free signup is
        anonymous. This is the flag that keeps that bounded."""
        self.assertFalse(plans.tier("free").connects_mailbox)

    def test_every_paid_tier_may(self) -> None:
        for key in ("label", "roster", "catalogue"):
            with self.subTest(tier=key):
                self.assertTrue(plans.tier(key).connects_mailbox)

    def test_the_free_tier_may_now_send(self) -> None:
        """Changed deliberately. Free sends through the platform's SES identity, bounded
        by `open_conversations`, so that a tenant can watch a pitch go out and an answer
        come back before paying — which is the whole argument for the tier existing."""
        self.assertTrue(plans.tier("free").sends_enabled)

    def test_the_free_tier_is_still_bounded_by_its_conversation_cap(self) -> None:
        """The cap is what stops "free can send" meaning "free can send anything". No
        new mechanism — the existing meter is the ceiling."""
        self.assertEqual(plans.tier("free").open_conversations, 5)

    def test_connects_mailbox_never_exceeds_sends_enabled(self) -> None:
        """A tier that could connect a mailbox but not send from it would be selling a
        setting with no effect."""
        for tier in plans.TIERS:
            with self.subTest(tier=tier.key):
                if tier.connects_mailbox:
                    self.assertTrue(tier.sends_enabled)


@unittest.skipUnless(HAVE_DB, "needs DATABASE_URL")
class TheTokenIsMintedOnTheWayOut(unittest.TestCase):

    def setUp(self) -> None:
        import psycopg
        from psycopg.rows import dict_row
        from spindle import outreach

        self.conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True,
                                    row_factory=dict_row)
        self.tenant = str(uuid.uuid4())
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO tenant (id, slug, name) VALUES (%s, %s, %s)",
                        (self.tenant, f"test-verp-{self.tenant[:8]}", "verp test"))
        artist = self._party("A", "roster")
        curator = self._party("C", "counterparty")
        campaign = str(outreach.create_campaign(
            self.conn, self.tenant, party_id=artist, name="c")["id"])
        self.thread = str(outreach.open_thread(
            self.conn, self.tenant, campaign_id=campaign,
            counterparty_id=curator)["id"])

    def tearDown(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM tenant WHERE id = %s", (self.tenant,))
        self.conn.close()

    def _party(self, name: str, party_class: str) -> str:
        party_id = str(uuid.uuid4())
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO party (id, tenant_id, slug, name, party_class) "
                "VALUES (%s, %s, %s, %s, %s)",
                (party_id, self.tenant, f"{name}-{party_id[:8]}", name, party_class))
        return party_id

    def _token(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT reply_token FROM thread WHERE id = %s", (self.thread,))
            return cur.fetchone()["reply_token"]

    def test_a_thread_starts_with_no_token(self) -> None:
        """Minted on first send, not at open. A thread that never sends never needs an
        address, and minting early would put unused tokens in the unique index."""
        self.assertEqual(self._token(), "")

    def test_sending_mints_one(self) -> None:
        address = sender.reply_to_for(self.conn, self.tenant, self.thread,
                                      "spindle@example.com")
        self.assertNotEqual(self._token(), "")
        self.assertEqual(address, f"spindle+{self._token()}@example.com")

    def test_minting_twice_keeps_the_first_token(self) -> None:
        """A thread sends more than once — a follow-up, a re-send after a bounce. The
        address a curator already has in their mailbox must keep working."""
        first = sender.reply_to_for(self.conn, self.tenant, self.thread,
                                    "spindle@example.com")
        second = sender.reply_to_for(self.conn, self.tenant, self.thread,
                                     "spindle@example.com")
        self.assertEqual(first, second)

    def test_the_minted_address_round_trips_through_the_ladder(self) -> None:
        """The two halves meet: what `sender` puts in `Reply-To` is what `inbound` reads
        back out. Pinned together because they are in different modules and nothing else
        would catch them drifting."""
        address = sender.reply_to_for(self.conn, self.tenant, self.thread,
                                      "spindle@example.com")
        token = inbound.token_in((address,))

        found = inbound.deliver(self.conn, inbound.InboundMessage(
            provider_message_id=f"in-{uuid.uuid4()}",
            from_address="curator@example.org",
            to_addresses=(address,), subject="Re: pitch", body="yes"))

        self.assertEqual(found.thread_id, self.thread)
        self.assertEqual(found.matched_by, "reply_token")
        self.assertTrue(token)


if __name__ == "__main__":
    unittest.main()
