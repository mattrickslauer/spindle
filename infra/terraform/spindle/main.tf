# The platform console, as cheaply as AWS can be made to run a web app.
#
# Idle cost: $0.00. Not "under a dollar" — zero. Every line below is chosen to
# keep it there, and each omission is deliberate rather than forgotten:
#
#   * No API Gateway. A Lambda Function URL does the same job for a server-rendered
#     app and costs nothing per request. API Gateway is ~$1.00/million on top of
#     Lambda for routing we are not using.
#   * No VPC, and therefore no NAT gateway. A NAT is ~$32/month of pure idle floor,
#     which is more than everything else here combined, forever, whether or not
#     anyone visits. The Lambda reaches CockroachDB over the public internet with
#     verify-full TLS, which is how CockroachDB Cloud expects to be reached anyway.
#     (infra/README.md makes this same argument for the RemixKit stack.)
#   * No Secrets Manager. $0.40/secret/month for two secrets, when Lambda
#     environment variables are already encrypted at rest with an AWS-managed KMS
#     key at no charge. Revisit when something needs rotation without a deploy.
#   * No ECR. A zip deployment avoids per-GB image storage entirely.
#
# The list used to carry a fourth entry — "No CloudFront, no custom domain, no ACM. The
# Function URL is a working HTTPS endpoint. A domain is a $12/year decision to take when
# there is something worth pointing it at." **That decision was taken on 2026-08-16** and
# `domain.tf` is the result; what made it worth taking was outbound mail, which pays a
# deliverability cost on every send that carries an opaque `*.on.aws` link. CloudFront
# itself stays inside its perpetual free tier at this volume (1TB out, 10M requests a
# month) and ACM certificates are free, so the compute story above is unchanged.
#
# Idle cost is therefore no longer exactly $0.00. It is **$0.50/month**, all of it the
# Route 53 hosted zone `dns.tf` creates. That is the honest number and it is stated here
# rather than left to contradict the opening line.
#
# What does cost money, eventually: Lambda invocations beyond the free tier
# (1M requests + 400k GB-seconds per month, which this will not approach), and
# CloudWatch log ingestion, capped below by a 7-day retention.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }

  # Local state by default so a first `apply` needs nothing but credentials.
  # Uncomment before a second person ever runs this — two people applying against
  # local state is how infrastructure gets destroyed by accident.
  #
  # backend "s3" {
  #   bucket = "spindle-tfstate"
  #   key    = "prod/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.region

  # Every taggable resource this stack creates carries these, without any
  # resource having to remember to. This is what makes the platform's spend
  # separable from RemixKit's in Cost Explorer — group by the Project tag.
  default_tags {
    tags = {
      Project   = "spindle"
      Env       = var.env
      ManagedBy = "terraform"
      Repo      = "respect-the-funk"
      Component = "console"
    }
  }
}

# CloudFront reads viewer certificates from us-east-1 only, whatever region the stack
# runs in. `var.region` is us-east-1 today, so this alias resolves to the same place as
# the provider above — it exists so that moving the stack to another region does not
# silently produce a certificate CloudFront will refuse to attach. `domain.tf` is the
# only consumer.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = "spindle"
      Env       = var.env
      ManagedBy = "terraform"
      Repo      = "respect-the-funk"
      Component = "console"
    }
  }
}

locals {
  # **Deliberately still `rtf-platform`, after the package was renamed to `spindle`.**
  #
  # This looks like a leftover and is not. Every resource in this stack derives its name
  # from here, and an AWS resource name is part of its identity: changing it is not a
  # rename, it is a destroy and a create. Following the package rename here produced a
  # plan of 23 to add and 22 to destroy, which would have meant
  #
  #   * a **new Function URL** — `console_url` is the deployed address, and the old one
  #     stops answering the moment the Lambda is replaced;
  #   * a **replaced masters bucket**. `aws_s3_bucket.masters` has no `force_destroy` and
  #     the live bucket is not empty, so the destroy fails with `BucketNotEmpty` — *after*
  #     Terraform has already torn down the log groups, the IAM roles and the function.
  #     The failure mode is not "apply refused", it is "apply stopped halfway";
  #   * a new ECR repository, leaving the classifier image behind in the old one.
  #
  # None of that buys anything. What the rename was for — one name for the codebase — is
  # a property of the repository, and the tag below already carries the new name for
  # anyone filtering the console. Renaming the resources is a migration with a data copy,
  # a URL change and an image re-push in it, and it should be done as that, on purpose,
  # rather than as a side effect of a `sed` over the source tree.
  #
  # If it is ever done: copy the masters objects to the new bucket first, add
  # `force_destroy` or empty the old one, re-push the classifier image, and expect
  # `console_url` to change.
  name = "rtf-platform-${var.env}"
}

# ------------------------------------------------------------------ the bundle
# Built by ./build.sh, which vendors dependencies for arm64 before this runs.

data "archive_file" "bundle" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/.terraform-tmp/console.zip"
}

# -------------------------------------------------------------------- identity

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "console" {
  name               = "${local.name}-console"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.console.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The console signs presigned URLs, and a presigned URL carries the *signer's*
# authority — it is not a capability the bucket grants to the URL holder. So the
# permissions below are exactly what an upload and a playback need, and the browser
# never holds credentials of its own.
#
# s3:ListBucket is here for a reason that is not listing. Without it, S3 answers a
# HEAD for a missing key with 403 instead of 404, because it will not confirm or deny
# the existence of an object to a principal that cannot enumerate the bucket. That
# would make `storage.head` unable to distinguish "the upload never finished" from
# "the IAM policy is wrong" — a misconfiguration reported to operators as their own
# mistake, forever. See that function's docstring.
data "aws_iam_policy_document" "masters" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.masters.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.masters.arn]
  }
}

# ---------------------------------------------------------------------------- SES
#
# Count-gated on `mail_domain` so a checkout with no domain plans and applies cleanly and
# creates nothing. That is the same shape the classifier and the changefeed use, and it
# keeps "unconfigured" a first-class state rather than a broken one.
#
# **Two things this cannot do for you, both named in docs/runbooks/ses-sign-in-mail.md:**
#
#   1. **Verification is DNS.** With `mail_route53_zone_id` set the CNAMEs below are
#      created and verification completes on its own. Without it, Terraform emits the
#      three records as an output and somebody adds them wherever the domain's DNS lives.
#      There is no third option and no way to skip it.
#   2. **A new SES account is in the sandbox**, which will only deliver to addresses that
#      have themselves been verified. That is a support request with a turnaround, it
#      cannot be expressed as a resource, and until it clears **sign-in works only for
#      addresses verified in the AWS console.** On a deployment where sign-in is the only
#      door, that is worth knowing before the first person tries it rather than after.

resource "aws_ses_domain_identity" "mail" {
  count  = var.mail_domain == "" ? 0 : 1
  domain = var.mail_domain
}

resource "aws_ses_domain_dkim" "mail" {
  count  = var.mail_domain == "" ? 0 : 1
  domain = aws_ses_domain_identity.mail[0].domain
}

# Three CNAMEs, which is what DKIM signing needs. Created only when the zone is here to
# create them in; otherwise `ses_dkim_records` in outputs.tf carries them out.
resource "aws_route53_record" "ses_dkim" {
  count   = var.mail_domain == "" || !local.mail_zone_known ? 0 : 3
  zone_id = local.mail_zone_id
  name    = "${aws_ses_domain_dkim.mail[0].dkim_tokens[count.index]}._domainkey.${var.mail_domain}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_ses_domain_dkim.mail[0].dkim_tokens[count.index]}.dkim.amazonses.com"]
}

# The console may send, and only from the verified identity. Scoped to the identity ARN
# rather than "*" so a bug — or a stolen set of credentials — cannot send as a domain this
# deployment does not own, which is the difference between an incident and a spam
# incident somebody else's reputation pays for.
# A custom MAIL FROM subdomain, which is the only thing that makes SPF *align*.
#
# ## Why this exists, and why adding `include:amazonses.com` to SPF would not have worked
#
# Sign-in codes landed in Gmail's spam folder on 2026-08-15. DKIM was verified, aligned
# and passing, so DMARC passed on the DKIM half — but SPF did not align, and that is a
# real negative signal rather than a cosmetic one.
#
# The reason is the *envelope* sender. Without a custom MAIL FROM, SES sends with a
# Return-Path at `*.amazonses.com`, so the receiver's SPF check authenticates
# **amazonses.com** — which passes, and is a different domain from the one in the `From`
# header. DMARC only counts SPF when the two align. This is the standard trap: putting
# `include:amazonses.com` in mattrickslauer.com's SPF record changes nothing, because
# that record is never the one consulted.
#
# Setting a MAIL FROM subdomain moves the Return-Path to `bounce.<domain>`, which does
# align, and the SPF record published there is then the one that is checked.
#
# ## `UseDefaultValue`, deliberately
#
# `RejectMessage` is the stricter setting and it is the wrong one to apply here first.
# The DNS for this domain is at the registrar, not in Route 53, so the two records below
# have to be added by hand — and between this resource applying and those records
# existing, `RejectMessage` would refuse **every** send. On a deployment where an emailed
# code is the only credential, that is a self-inflicted lockout in the window where
# somebody is least likely to be watching.
#
# With `UseDefaultValue`, SES falls back to `*.amazonses.com` until the records resolve:
# exactly today's behaviour, no worse, and it upgrades itself the moment DNS is right.
# Tighten to `RejectMessage` once `aws ses get-identity-mail-from-domain-attributes`
# reports `Success` — that is a one-line change and it is worth making, because at that
# point a fallback to an unaligned envelope is a silent regression of this whole fix.
#
# The records to add are `terraform output ses_mail_from_records`; the runbook has them
# with the reasoning.
resource "aws_ses_domain_mail_from" "mail" {
  count                  = var.mail_domain == "" ? 0 : 1
  domain                 = aws_ses_domain_identity.mail[0].domain
  mail_from_domain       = "bounce.${var.mail_domain}"
  behavior_on_mx_failure = "UseDefaultValue"
}

# `console_send_mail` (ses:SendEmail on the identity) was here until 2026-10-19. The
# console now sends through Resend — see the `resend` data source below and mail.py —
# so the role needs no SES authority. The identity, DKIM and MAIL FROM resources above
# stay: deleting them would tear DNS records out from under a verified domain for no
# gain, and they cost nothing.

# ------------------------------------------------------------------------- Resend
#
# The platform mail transport's key, shared across projects at one SSM SecureString.
# Read at plan time and passed in the function's environment — the same route
# `stripe_secret_key` takes, so the value lands in state and in the (KMS-encrypted)
# Lambda configuration exactly as Stripe's does. The parameter must exist before a plan:
#
#   aws ssm put-parameter --name /resend/api-key --type SecureString --value "re_…"
#
# Empty `resend_api_key_parameter` skips the lookup; `mail.load()` then refuses by name.
data "aws_ssm_parameter" "resend" {
  count           = var.resend_api_key_parameter == "" ? 0 : 1
  name            = var.resend_api_key_parameter
  with_decryption = true
}

resource "aws_iam_role_policy" "masters" {
  name   = "${local.name}-masters"
  role   = aws_iam_role.console.id
  policy = data.aws_iam_policy_document.masters.json
}

# ---------------------------------------------------------------------- masters
#
# The first resource in this stack that is not free at idle. Everything above is
# priced per invocation and costs nothing when nobody visits; a bucket is priced per
# GB-month and costs the same whether or not anyone ever plays a master. At
# $0.023/GB/month, the current roster — two albums — is a rounding error, and it is
# still worth naming the change of kind rather than letting the first non-zero line
# on a bill be a surprise.
#
# S3 is also the second qualifying AWS service in the CockroachDB × AWS hackathon
# submission, where today there is only Lambda. That is a consequence and not the
# reason: masters need somewhere to live regardless.

resource "aws_s3_bucket" "masters" {
  # Bucket names are globally unique across every AWS account, so this can collide
  # with a stranger's. That surfaces as `BucketAlreadyExists` at apply time — loud,
  # immediate, and fixed by changing `env` or this suffix. An account-id suffix would
  # prevent it at the cost of an unreadable name in every console listing and log
  # line; a failure this legible does not need preventing.
  bucket = "${local.name}-masters"

  tags = { Component = "masters" }
}

# Masters are unreleased audio. Nothing about this bucket is public, and the account
# level setting is not assumed to be on — this is per-bucket and explicit, because a
# public masters bucket is the single worst outcome available here and it is usually
# reached by inheriting a default rather than by anyone choosing it.
resource "aws_s3_bucket_public_access_block" "masters" {
  bucket                  = aws_s3_bucket.masters.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 is applied by default on new buckets, which is exactly why it is written out
# here: a default is a thing that can change, and `terraform plan` showing a drift on
# an explicit resource is how that would be noticed. SSE-KMS would add $1/month for
# the key plus a request charge, and buys nothing this threat model needs — the
# objects are already unreachable without a signature from the console's role.
resource "aws_s3_bucket_server_side_encryption_configuration" "masters" {
  bucket = aws_s3_bucket.masters.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning, because the delete path here is genuinely lossy in a way nothing else
# in this stack is. `assets.delete` removes a row and hands the caller an object key;
# a mistaken click deletes the only copy of a master the label may not have elsewhere.
# Noncurrent versions expire after 30 days (below), so this buys a month of undo
# rather than an unbounded archive.
resource "aws_s3_bucket_versioning" "masters" {
  bucket = aws_s3_bucket.masters.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "masters" {
  bucket = aws_s3_bucket.masters.id

  # The rule that pays for itself. A browser PUT of a 90MB master that dies partway
  # leaves an incomplete multipart upload: invisible in every listing, and billed for
  # its parts indefinitely. This is the single most common way an S3 bill grows
  # without a corresponding object, and seven days is longer than any upload here.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # The undo window described above. Thirty days of noncurrent versions, then gone —
  # long enough that a deletion noticed at the next monthly statement is still
  # recoverable, short enough that superseded masters do not accumulate forever.
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.masters]
}

# CORS, without which the whole presigned design silently does not work in a browser.
#
# The PUT goes from the page to `bucket.s3.amazonaws.com`, which is a different origin
# from the Function URL. The browser sends a preflight OPTIONS first, and S3 answers
# it from this configuration and not from the presigned signature — so a correctly
# signed URL fails with an opaque CORS error that names neither S3 nor the missing
# rule. It reads like broken JavaScript.
#
# `ETag` is exposed because it is the one response header the uploader reads back.
resource "aws_s3_bucket_cors_configuration" "masters" {
  bucket = aws_s3_bucket.masters.id

  cors_rule {
    allowed_methods = ["PUT", "GET", "HEAD"]
    # `compact` because the custom domain is optional: with `dns_domain` unset,
    # `local.console_host` is "" and an empty string in this list is not a permissive
    # origin, it is a malformed one that S3 rejects on apply.
    allowed_origins = compact([
      # The console itself. `function_url` carries a trailing slash and an Origin
      # header never does, so an untrimmed value matches nothing at all.
      trimsuffix(aws_lambda_function_url.console.function_url, "/"),
      # The same console under its custom domain. Both entries are live at once and
      # deliberately so — the Function URL keeps answering until an Origin Access
      # Control closes it (see domain.tf), and dropping it from this list before then
      # breaks uploads from the address the submission docs still advertise.
      local.dns_enabled ? "https://${local.console_host}" : "",
      # `dev.sh`, so the upload path can be exercised before it is deployed. This is
      # the one origin here that is not ours, and it is bounded: an attacker who can
      # serve from a victim's localhost:8000 already has the machine.
      "http://localhost:8000",
    ])
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# --------------------------------------------------------------------- compute

# Created explicitly rather than left to Lambda's implicit creation, because an
# implicitly created log group retains forever and log retention is the one line
# item here that grows without anyone deciding it should.
resource "aws_cloudwatch_log_group" "console" {
  name              = "/aws/lambda/${local.name}-console"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "console" {
  function_name = "${local.name}-console"
  role          = aws_iam_role.console.arn
  handler       = "spindle.handler.handler"

  # Must stay in lockstep with build.sh's --python-version. Wheels fetched for one
  # minor version and run on another is the failure mode this pairing prevents.
  # Note the dev machine runs 3.14, which Lambda does not offer — so anything
  # version-sensitive is only truly proven once it runs in the deployed function.
  runtime = "python3.13"

  # Graviton: ~20% cheaper per GB-second than x86 for identical work.
  architectures = ["arm64"]

  # 512MB is not about memory. Lambda scales CPU with memory, and a cold start
  # that imports FastAPI is CPU-bound — 512 measurably beats 256 on wall-clock
  # for less money, because it finishes proportionally sooner.
  memory_size = var.memory_mb
  timeout     = 15

  filename         = data.archive_file.bundle.output_path
  source_code_hash = data.archive_file.bundle.output_base64sha256

  # -1 by default: unreserved. See variables.tf for why a reservation is not
  # available on this account, and why the account's own 10-execution limit is a
  # stricter ceiling than the one this was trying to set.
  reserved_concurrent_executions = var.max_concurrency

  environment {
    variables = {
      DATABASE_URL = var.database_url

      # PLATFORM_ADMIN_TOKEN was set here until 2026-08-15 and is gone with the operator
      # role. Sign-in is an emailed code; `auth.py` records what the shared secret was
      # buying and what its removal cost.
      #
      # PLATFORM_TENANT_SLUG stays, doing only its other job: naming the tenant whose
      # figures the landing page, the manual and `GET /demo/r1` cite. No signed-in request
      # is scoped by it any more.
      PLATFORM_TENANT_SLUG = var.tenant_slug

      # The three the sender needs, and now the three sign-in needs. `mail_configured` is
      # all-or-nothing on purpose (settings.py argues it): a verified sender with no
      # postal address is a message that violates CAN-SPAM, and a sender with no reply-to
      # is one a curator cannot answer. Empty here means the console refuses to issue
      # sign-in codes and names these variables on the page, rather than sending nothing
      # and reporting success.
      PLATFORM_MAIL_SENDER         = var.mail_sender
      PLATFORM_MAIL_REPLY_TO       = var.mail_reply_to
      PLATFORM_MAIL_POSTAL_ADDRESS = var.mail_postal_address
      # The transport for the address above. See the `resend` data source.
      RESEND_API_KEY = (
        length(data.aws_ssm_parameter.resend) > 0
        ? data.aws_ssm_parameter.resend[0].value
        : ""
      )
      # Empty is a legitimate state in a checkout that has never applied this stack,
      # and `settings.storage_configured` reports it. Here it is always set, so the
      # console offers uploads. AWS_REGION is provided by the runtime itself, which
      # is why `settings.load` reads it and no region is passed here.
      PLATFORM_MASTERS_BUCKET = aws_s3_bucket.masters.bucket

      # The origin for any URL this deployment puts in front of somebody who is not
      # holding a session — today that is the listen links in outbound pitches. It must
      # be absolute: the recipient is reading it in a mail client, where a relative path
      # resolves against nothing.
      #
      # Empty until `dns_domain` is set, and empty is checked rather than defaulted.
      # `listen.py` raises when asked to mint a link without it instead of falling back
      # to the Function URL, because a pitch is not the place to discover that this
      # deployment does not know its own name.
      PLATFORM_PUBLIC_BASE_URL = local.dns_enabled ? "https://${local.console_host}" : ""

      # The console does not invoke the classifier — `analyse_recording` runs in a
      # worker, not in this function. It is set here so the console can *report* the
      # difference between "this track has no genre because nothing has listened to
      # it" and "because no classifier is deployed", which `settings.classifier_
      # configured` is for. A track with no genre and no explanation sends an operator
      # looking for a bug in the upload.
      #
      # Conditional because the classifier itself is `count`-gated on an image
      # existing. Referencing [0] unconditionally would fail to plan on a fresh
      # checkout, which is the state this whole stack is meant to apply cleanly from.
      PLATFORM_CLASSIFIER_FUNCTION = (
        length(aws_lambda_function.classifier) > 0
        ? aws_lambda_function.classifier[0].function_name
        : ""
      )

      # Stripe. Absent from this block entirely until 2026-08-15, which meant billing
      # was unconfigured on every deploy no matter what the operator's .env said — the
      # console read four variables Terraform never passed. See variables.tf.
      #
      # Both price pairs are passed unconditionally and only one is ever read;
      # `settings.stripe_mode` picks between them off the key's prefix. Passing both
      # costs nothing — an unused price id is an inert string — and it means promoting
      # this function to live is a change to `stripe_secret_key` in tfvars rather than
      # an edit to this file.
      STRIPE_SECRET_KEY        = var.stripe_secret_key
      STRIPE_WEBHOOK_SECRET    = var.stripe_webhook_secret
      STRIPE_PRICE_LABEL       = var.stripe_price_label
      STRIPE_PRICE_ROSTER      = var.stripe_price_roster
      STRIPE_PRICE_LABEL_LIVE  = var.stripe_price_label_live
      STRIPE_PRICE_ROSTER_LIVE = var.stripe_price_roster_live
    }
  }

  depends_on = [aws_cloudwatch_log_group.console]
}

# ------------------------------------------------------------------------- URL

# authorization_type NONE because the app authenticates its own callers: anonymous
# principals may read, and writes require the operator cookie. IAM auth here would
# make the console unreachable by a browser, which defeats the point of a demo URL.
resource "aws_lambda_function_url" "console" {
  function_name      = aws_lambda_function.console.function_name
  authorization_type = "NONE"
}

# authorization_type = "NONE" on the URL is necessary but NOT sufficient: it says
# "do not require SigV4", while the function's resource policy still has to allow
# the call. Without this the URL answers every request with
# {"Message":"Forbidden"}, which reads like a bug in the app and is not.
#
# `principal = "*"` is what public means here, and public is the intent — the
# console authenticates its own callers (anonymous reads, operator-token writes),
# so the authorization that matters happens in the app, not at the edge.
resource "aws_lambda_permission" "public_url" {
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.console.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# BOTH permissions are required, and this is the one every guide omits. With only
# the InvokeFunctionUrl grant above, the URL answers {"Message":"Forbidden"} with
# x-amzn-ErrorType: AccessDeniedException and the function is never invoked — no
# log stream is even created, so it reads like a networking fault rather than an
# authorization one. Verified by comparing against two working function URLs in
# this account: both carry this second statement.
#
# The narrower condition those two use — Bool lambda:InvokedViaFunctionUrl = true —
# is not expressible here: the AddPermission API rejects function_url_auth_type on
# the InvokeFunction action, and aws_lambda_permission exposes no arbitrary
# condition. The exposure that leaves is bounded: this function is deliberately
# public at the URL already, and all real authorization is in the app (anonymous
# reads, operator-token writes). Narrow it if that ever stops being true.
resource "aws_lambda_permission" "public_invoke" {
  statement_id  = "AllowInvokeViaFunctionUrl"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.console.function_name
  principal     = "*"
}

# ------------------------------------------------------------------- classifier
#
# Genre classification, per docs/2026-08-10-masters-and-classification.md §3b: a
# container-image Lambda rather than a spot instance, because the workload is seconds of
# CPU per track triggered by an upload, and the submission's "scales to zero, $0 idle"
# claim is measured and true. An always-on box to classify a handful of tracks would
# contradict a claim made on camera.
#
# x86_64, unlike everything else here. `essentia-tensorflow` publishes manylinux wheels
# for x86_64 only and there is no aarch64 build; forcing Graviton would mean compiling
# Essentia and TensorFlow from source for a function that runs seconds per month.

resource "aws_ecr_repository" "classifier" {
  name                 = "${local.name}-classifier"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # The image is ~1.5GB, most of it TensorFlow. ECR storage is $0.10/GB/month, so this
  # is ~$0.15/month for one image — the second non-zero line item in this stack after
  # the masters bucket, and worth naming rather than discovering.
  tags = { Component = "classifier" }
}

# Without this, every pushed image is kept forever and the bill grows by $0.15/month per
# build. Ten is enough to roll back through a bad afternoon.
resource "aws_ecr_lifecycle_policy" "classifier" {
  repository = aws_ecr_repository.classifier.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_iam_role" "classifier" {
  count              = var.classifier_image_uri == "" ? 0 : 1
  name               = "${local.name}-classifier"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "classifier_logs" {
  count      = var.classifier_image_uri == "" ? 0 : 1
  role       = aws_iam_role.classifier[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Read-only on the masters bucket. The classifier downloads an object and returns
# labels; it never writes one, so GetObject and nothing else. ListBucket is granted for
# the same reason the console has it — without it a missing key answers 403 rather than
# 404, and "the object is not there" becomes indistinguishable from "the policy is wrong".
data "aws_iam_policy_document" "classifier_read" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.masters.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.masters.arn]
  }
}

resource "aws_iam_role_policy" "classifier_read" {
  count  = var.classifier_image_uri == "" ? 0 : 1
  name   = "${local.name}-classifier-read"
  role   = aws_iam_role.classifier[0].id
  policy = data.aws_iam_policy_document.classifier_read.json
}

resource "aws_cloudwatch_log_group" "classifier" {
  count             = var.classifier_image_uri == "" ? 0 : 1
  name              = "/aws/lambda/${local.name}-classifier"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "classifier" {
  count         = var.classifier_image_uri == "" ? 0 : 1
  function_name = "${local.name}-classifier"
  role          = aws_iam_role.classifier[0].arn
  package_type  = "Image"
  image_uri     = var.classifier_image_uri
  architectures = ["x86_64"]

  # 3008MB is not about headroom. Lambda scales CPU with memory, and TensorFlow graph
  # loading plus inference is entirely CPU-bound — at 1024MB the cold start is minutes
  # and costs more in GB-seconds than 3008MB does, because it runs proportionally
  # longer. This is the cheaper number as well as the faster one.
  memory_size = 3008

  # Five minutes covers a cold start (~15s of TensorFlow import and graph load) plus the
  # download and inference of a long master, with room for a 200MB file on a slow day.
  timeout = 300

  # /tmp defaults to 512MB and the master is downloaded there. A 24-bit/96kHz master can
  # exceed that on its own, and the failure is an ENOSPC from inside boto3 that names
  # nothing about masters.
  ephemeral_storage {
    size = 2048
  }

  depends_on = [aws_cloudwatch_log_group.classifier]
}

# Let the console's role invoke it. The console does not call the classifier today —
# `analyse_recording` runs in a worker — but the worker uses whatever credentials it is
# given, and this is the role that exists to be given.
data "aws_iam_policy_document" "invoke_classifier" {
  count = var.classifier_image_uri == "" ? 0 : 1
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.classifier[0].arn]
  }
}

resource "aws_iam_role_policy" "invoke_classifier" {
  count  = var.classifier_image_uri == "" ? 0 : 1
  name   = "${local.name}-invoke-classifier"
  role   = aws_iam_role.console.id
  policy = data.aws_iam_policy_document.invoke_classifier[0].json
}

# ------------------------------------------------------------------- changefeed
#
# The webhook sink for `CREATE CHANGEFEED FOR TABLE thread, outbox, message`. This is the
# endpoint that makes `infra/platform_architecture.py`'s dashed arrow — "a changefeed
# wakes an agent" — a true sentence rather than a drawing of an intention. As of the
# 2026-08-10 audit, `SHOW CHANGEFEED JOBS` returned zero rows.
#
# `spindle/changefeed.py` is the whole of it; read that module's docstring before
# changing anything here. Two facts this stack has to respect:
#
#   * A wake is permission to look, not a grant of work. This function receives a batch,
#     turns it into "there may be claimable leads of these kinds in this tenant", and
#     calls `fleet.work_once`, which claims under a lease token exactly as every other
#     worker does. Webhook sinks are at-least-once by specification; two invocations for
#     one row change are normal, and they produce one unit of work because of the claim,
#     not because of anything in this file.
#   * Creating the changefeed itself is NOT done here. A changefeed is a job that draws
#     request units continuously against a $10 budget, and this stack has no CockroachDB
#     provider. `python -m spindle.changefeed --dry-run` prints the exact statement;
#     a human runs it once the URL below exists and the budget has been checked.
#
# Same zip as the console — `data.archive_file.bundle` already contains `spindle` —
# so this adds a function and not a build step. Idle cost is zero for the reasons argued
# at the top of this file. Cost in use is one invocation per *batch*, because the feed is
# created with `webhook_sink_config` flushing at 50 messages or 5 seconds, plus one
# invocation per `resolved` heartbeat (every 5 minutes, 288 a day, answered without a
# database connection).

resource "aws_cloudwatch_log_group" "changefeed" {
  count             = var.changefeed_webhook_token == "" ? 0 : 1
  name              = "/aws/lambda/${local.name}-changefeed"
  retention_in_days = var.log_retention_days
}

# Logs and nothing else. This function claims leads and runs agents; the one agent a wake
# can reach today (`distil_lesson`) talks to an embedding provider over HTTPS with a key
# from the environment and needs no AWS authority at all.
#
# The Sender is deliberately NOT reachable from here: no PLATFORM_MAIL_* variables are set
# on *this* function, so `settings.mail_configured` is false in the changefeed and
# `changefeed.lambda_handler` reports an outbox wake as "no sender attached" rather than
# mailing a curator because a row appeared. `ingest.py` keeps `--send` separate from
# draining for that reason and a changefeed must not quietly undo it.
#
# **This note used to end "wiring SES here is a decision to take on purpose". That decision
# was taken on 2026-08-15, and only half of it.** SES is now wired to the *console*
# function, because email-OTP sign-in made mail the only way anybody gets in. It is
# deliberately not wired here, and the distinction is the whole point of leaving this
# paragraph rather than deleting it: a person signing in asks for the message they are
# about to receive, and a curator does not. The console may send because a human just
# clicked; the changefeed may not send because a row appeared.
resource "aws_iam_role" "changefeed" {
  count              = var.changefeed_webhook_token == "" ? 0 : 1
  name               = "${local.name}-changefeed"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "changefeed_logs" {
  count      = var.changefeed_webhook_token == "" ? 0 : 1
  role       = aws_iam_role.changefeed[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "changefeed" {
  count         = var.changefeed_webhook_token == "" ? 0 : 1
  function_name = "${local.name}-changefeed"
  role          = aws_iam_role.changefeed[0].arn
  handler       = "spindle.changefeed.lambda_handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = var.memory_mb

  # Longer than the console's 15s, and for a different reason. The console renders a page;
  # this claims a lead and runs an agent, and `distil_lesson` makes an embedding call. A
  # timeout shorter than the work means the batch is never acknowledged, the feed retries
  # it, and the same work is fetched again — paid for twice, completed once. Sixty seconds
  # comfortably covers a batch and is well under `fleet.LEASE_SECONDS` (120), so a
  # function killed at the limit still leaves a lease that expires while somebody is
  # watching rather than one that outlives the demo.
  timeout = 60

  filename         = data.archive_file.bundle.output_path
  source_code_hash = data.archive_file.bundle.output_base64sha256

  reserved_concurrent_executions = var.max_concurrency

  environment {
    variables = {
      DATABASE_URL = var.database_url

      # The shared secret the feed sends as `webhook_auth_header`. The sink is a public
      # Function URL — CockroachDB cannot sign SigV4 — so this header is the entire
      # boundary. `changefeed._secret()` refuses every delivery when it is unset, which is
      # why this whole endpoint is count-gated on the same variable rather than defaulted.
      PLATFORM_CHANGEFEED_TOKEN = var.changefeed_webhook_token

      # The name leases are taken under. Distinct from `ingest-cli` on purpose:
      # `lead.owner_agent` is what an operator reads to find out which worker holds a row,
      # and "a changefeed woke this" and "somebody ran the CLI" are different answers to
      # that question.
      PLATFORM_CHANGEFEED_WORKER = "changefeed-lambda"

      PLATFORM_TENANT_SLUG = var.tenant_slug
    }
  }

  depends_on = [aws_cloudwatch_log_group.changefeed]
}

# NONE for the same reason the console's is NONE, with a different authenticator behind
# it: CockroachDB's webhook sink cannot sign SigV4, so IAM auth here would make the
# endpoint unreachable by the only client it has. `changefeed.authenticate` compares the
# shared header with `hmac.compare_digest` *before* anything opens a database connection,
# so an unauthenticated flood costs a Lambda invocation and not one request unit.
resource "aws_lambda_function_url" "changefeed" {
  count              = var.changefeed_webhook_token == "" ? 0 : 1
  function_name      = aws_lambda_function.changefeed[0].function_name
  authorization_type = "NONE"
}

# Both statements, for the reason spelled out against the console's pair above: the URL's
# auth type is necessary and not sufficient, and with only the first the endpoint answers
# {"Message":"Forbidden"} without ever invoking the function — which reads as a networking
# fault and is not one.
resource "aws_lambda_permission" "changefeed_url" {
  count                  = var.changefeed_webhook_token == "" ? 0 : 1
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.changefeed[0].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "changefeed_invoke" {
  count         = var.changefeed_webhook_token == "" ? 0 : 1
  statement_id  = "AllowInvokeViaFunctionUrl"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.changefeed[0].function_name
  principal     = "*"
}
