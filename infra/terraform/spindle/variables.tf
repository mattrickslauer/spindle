variable "region" {
  type        = string
  default     = "us-east-1"
  description = "Same region as the CockroachDB cluster (aws-us-east-1) and Bedrock. Cross-region would add latency to every query and egress to every embedding."
}

variable "env" {
  type    = string
  default = "prod"
}

variable "database_url" {
  type        = string
  sensitive   = true
  description = "CockroachDB connection string. From .env — never committed."
}

# `admin_token` was declared here until 2026-08-15. It was the shared operator secret,
# and it is gone with the operator role: sign-in is an emailed six-digit code and nothing
# else. Setting PLATFORM_ADMIN_TOKEN on the function now does nothing at all.
#
# If it is still in your terraform.tfvars, delete the line — that file is gitignored and
# holds the only copy of a secret that no longer authenticates anything.

variable "tenant_slug" {
  type = string
  # NOT the tenant a signed-in request is scoped to. That comes off the caller's own
  # `account` row now, and `routes._tenant_id` has no fallback left.
  #
  # What survives is the *showcase* tenant: the one whose counters the landing page
  # prints, whose two figures the manual cites in prose, and which `GET /demo/r1` answers
  # from so a judge with no credential can reproduce R1. `routes._showcase_tenant_id` is
  # the only reader, and it is a separate function from `_tenant_id` precisely so this
  # cannot drift back into being an authentication fallback.
  default = "respect-the-funk"
}

# ------------------------------------------------------------------------- mail
#
# **Mail is now the only way anybody signs in.** Until 2026-08-15 the sender was
# deliberately unwired — see the note above `aws_iam_role.changefeed` in main.tf, which
# still stands for the outbox worker — and `settings.mail_configured` was false, so
# nothing could mail a curator by accident. Email-OTP sign-in changes the stakes: a
# deployment with no verified SES identity is one nobody can enter, because `otp.py`
# refuses to issue a code rather than falling back to showing one.
#
# All four default to empty, and empty is still a legitimate state — it plans and applies
# cleanly, creates no SES resources, and yields a console whose sign-in page names the
# three variables it needs. What it does not do is pretend.

variable "mail_domain" {
  type        = string
  description = "Domain to verify with SES, e.g. respectthefunk.com. Empty creates no SES resources at all."
  default     = ""
}

variable "mail_route53_zone_id" {
  type        = string
  description = <<-EOT
    Route 53 hosted zone for mail_domain. Set it and the DKIM CNAMEs are created for you;
    leave it empty and they are emitted as the `ses_dkim_records` output for you to add
    wherever the domain's DNS actually lives.

    Empty is not a lesser path — plenty of domains are not in Route 53 — but it does mean
    verification is a manual step, and the runbook says so.
  EOT
  default     = ""
}

variable "mail_sender" {
  type        = string
  description = "The From address, e.g. spindle@mattrickslauer.com. Its domain must be verified in Resend, or every send is refused (403)."
  default     = ""
}

variable "mail_reply_to" {
  type        = string
  description = "Where a reply goes. Required: mail.py refuses to construct a mailer without one, because a pitch a curator cannot reply to is worse than no pitch."
  default     = ""
}

variable "mail_postal_address" {
  type        = string
  description = "Physical postal address. CAN-SPAM 7704(a)(5) requires it in every commercial message; mail.compliant_body appends it. Sign-in codes are transactional and do NOT carry it — see otp.py."
  default     = ""
}

# ------------------------------------------------------------------------ Stripe
#
# Nothing Stripe-related reached the deployed function until 2026-08-15. The console
# read four variables that Terraform never set, so `settings.stripe_configured` was
# false on every deploy and the pricing page said billing was unconfigured — correctly,
# and for a reason nobody had written down. These are that omission fixed.
#
# All default to "" so a fresh `terraform apply` still works with no Stripe account at
# all. That is the same argument `mail_sender` makes: the free tier must not depend on a
# payment provider, and `POST /claim` works end to end with every one of these empty.
#
# The mode is not declared here. It is read off `stripe_secret_key`'s own prefix by
# `settings.stripe_mode`, so a live deployment is one whose tfvars holds an sk_live_ key
# — there is no separate flag that could be set to "test" beside a live credential.

variable "resend_api_key_parameter" {
  type        = string
  default     = "/resend/api-key"
  description = "SSM SecureString holding the Resend API key, the platform mail transport. Empty skips the lookup and the console refuses to send, naming RESEND_API_KEY."
}

variable "stripe_secret_key" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Stripe secret key. sk_test_ or sk_live_; the prefix is what selects the mode and the price pair. Empty means the console reports billing as unconfigured and refuses checkout by name."
}

variable "stripe_webhook_secret" {
  type      = string
  sensitive = true
  default   = ""

  # Per-endpoint, not per-account: this must be the secret of the endpoint pointing at
  # *this* function's /billing/webhook, and a secret borrowed from another endpoint on
  # the same account fails verification on both. Stripe reveals it only at creation.
  #
  # Empty refuses every delivery rather than trusting an unsigned body, which is the
  # only safe reading — an unverified billing webhook is an unauthenticated way to
  # grant paid plans.
  description = "Signing secret (whsec_) for this deployment's own webhook endpoint."
}

variable "stripe_price_label" {
  type        = string
  default     = ""
  description = "Test-mode price id for the Label tier. Read only when the key is sk_test_."
}

variable "stripe_price_roster" {
  type        = string
  default     = ""
  description = "Test-mode price id for the Roster tier. Read only when the key is sk_test_."
}

variable "stripe_price_label_live" {
  type        = string
  default     = ""
  description = "Live-mode price id for the Label tier. Read only when the key is sk_live_."
}

variable "stripe_price_roster_live" {
  type        = string
  default     = ""
  description = "Live-mode price id for the Roster tier. Read only when the key is sk_live_."
}

variable "memory_mb" {
  type    = number
  default = 512
}

variable "max_concurrency" {
  type    = number
  default = -1

  # -1 means "no reservation", which is the only value this account can accept.
  #
  # The intent was a hard per-function ceiling so a runaway loop could not bill AWS
  # and CockroachDB unbounded. It cannot be had here: this account's total Lambda
  # concurrency is 10 (AWS's reduced default; the usual limit is 1000), and AWS
  # refuses any reservation that leaves fewer than 10 unreserved. Reserving even 1
  # is therefore rejected.
  #
  # The protection is not lost, only relocated — the account limit of 10 is itself a
  # stricter ceiling than the 10 this variable was trying to impose. What *is* lost
  # is isolation: this console shares those 10 with the other functions in the
  # account, RemixKit's included. At the console's traffic that is not a real risk,
  # but it is the reason to raise the account quota before anything here gets busy.
  description = "Reserved concurrency. -1 leaves the function unreserved, sharing the account pool."
}

variable "log_retention_days" {
  type        = number
  default     = 7
  description = "CloudWatch charges for ingestion and storage. Seven days is enough to debug a deploy and cheap enough to ignore."
}

variable "classifier_image_uri" {
  type    = string
  default = ""

  # Empty by default, and the classifier Lambda is `count = 0` while it is empty.
  #
  # That gate exists because a container-image Lambda cannot be created before the image
  # is pushed — Terraform would fail on an ECR repository it had just created and that
  # contains nothing. The ECR repository itself is always created, because it has to
  # exist before anything can be pushed to it. So the order is: apply (repo appears),
  # push the image, set this to the digest URI, apply again.
  #
  # Use the @sha256 digest form rather than a :tag. Lambda resolves a tag once at create
  # time and never again, so re-pushing :latest leaves the function silently running the
  # old image while every console and plan says it is current.
  description = "Digest URI of the pushed classifier image. Empty leaves the classifier undeployed; the console works without it and says so."
}

variable "changefeed_webhook_token" {
  type      = string
  sensitive = true
  default   = ""

  # Empty by default, and the whole changefeed endpoint is `count = 0` while it is — the
  # same gate the classifier image uses, for a stronger reason.
  #
  # The sink is a public Function URL. CockroachDB's webhook sink cannot sign SigV4, so
  # the only thing standing between a stranger and the ability to wake this fleet as often
  # as they like is the `webhook_auth_header` this value becomes. An endpoint deployed
  # without one authenticates nothing: `changefeed._secret()` refuses every delivery in
  # that state, which is fail-closed, but it leaves a live public URL that exists only to
  # answer 401. Not creating it at all is the better shape.
  #
  # Generate with `openssl rand -hex 32`. Set it here *and* in the CREATE CHANGEFEED
  # statement that `python -m spindle.changefeed --dry-run --reveal` prints — the two
  # must be the same string. If they drift, every delivery answers 401 and the feed
  # retries forever, which is the intended alarm and not a leak.
  description = "Shared secret the changefeed sends as webhook_auth_header. Empty leaves the webhook endpoint undeployed."
}
