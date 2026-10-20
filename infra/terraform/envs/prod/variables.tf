variable "region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region. B2 egress is billed regardless of this, so pick for latency."
}

variable "env" {
  type    = string
  default = "prod"
}

variable "tenant_id" {
  type        = string
  default     = "respect-the-funk"
  description = "Tenant #1 — the label that dogfoods the product. Partition key everywhere."
}

variable "b2_bucket" {
  type        = string
  description = "Backblaze B2 bucket holding artists, identities, songs, masters, kits, manifests."
}

variable "b2_region" {
  type        = string
  default     = ""
  description = "B2 S3 endpoint region, e.g. us-west-000. Confirm with b2_authorize_account; the default guess is wrong for many buckets."
}

variable "api_image_tag" {
  type    = string
  default = "api-latest"
}

variable "worker_image_tag" {
  type    = string
  default = "worker-latest"
}

# Backend selection. Defaults are production; override in terraform.tfvars to bring a
# console up before credentials exist. See modules/api for the reasoning.
variable "storage_backend" {
  type    = string
  default = "b2"
}

variable "generator_backend" {
  type    = string
  default = "genblaze"
}

variable "queue_backend" {
  type    = string
  default = "sqs"
}

# Sign-in. **On.** The console is behind email-OTP; `/` is the public landing page and
# is the only route that renders without a session.
#
# Both secrets must be in SSM *before* this is applied, because `require_auth` makes
# their absence fatal rather than silent:
#
#   1. aws ssm put-parameter --name /remixkit/prod/SESSION_SECRET  --type SecureString \
#        --value "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" --overwrite
#   2. aws ssm put-parameter --name /resend/api-key --type SecureString \
#        --value "re_…" --overwrite      # shared with other projects; may already exist
#
# Secrets first, in that order: a console that turns on auth before it can sign or send
# is one nobody can enter, including whoever needs to fix it. With `require_auth = true`
# that failure is a function that does not boot (deps.py `_session_secret`) rather than
# one quietly signing sessions with a key that changes on every cold start.
#
# To go back to an open console — only ever for a scratch environment — set
# auth_backend = "none" *and* require_auth = false. Either alone refuses to start.
variable "auth_backend" {
  type    = string
  default = "otp"
}

variable "require_auth" {
  type        = bool
  default     = true
  description = "Refuse to start rather than serve an open console that believes it is gated."
}

variable "mail_backend" {
  type    = string
  default = "resend"
}

variable "mail_from" {
  type = string
  # Must be on a domain verified in Resend (mattrickslauer.com is; agfarms.dev cannot be
  # added on the current plan). Resend refuses an unverified From outright, and the
  # console then says the code could not be sent.
  default = "rtp@mattrickslauer.com"
}

variable "gcp_project" {
  type        = string
  default     = ""
  description = "Vertex project for the Google (Veo/Imagen) fallback. Inert until a service-account credential is in SSM."
}

variable "gcp_location" {
  type    = string
  default = "us-central1"
}

variable "allowed_emails" {
  type = list(string)
  # This is the user table. There is no sign-up: an address either appears here or it is
  # refused before a code is ever sent, so adding a colleague is a reviewed line in this
  # file rather than a button in the console. Empty refuses to start under `otp` —
  # a console nobody can enter is a failure, not a safe default.
  default     = ["anthonybtedesco@gmail.com"]
  description = "Addresses permitted to sign in. This is the user table."
}
