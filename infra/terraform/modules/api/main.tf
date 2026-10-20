# The web tier: Lambda from a container image, fronted by a Function URL.
#
# infra/README ①. No API Gateway — Function URLs are free and sufficient, and a
# gateway in front of a single-service app is a second thing to configure for no
# benefit. No provisioned concurrency, so idle is genuinely $0.
#
# This Lambda serves JSON and HTML only. It never touches asset bytes: uploads and
# downloads are presigned browser↔B2 (§2b rule 2), which is also why a 6 MB Lambda
# response limit is not a constraint here.

variable "name_prefix" { type = string }
variable "env" { type = string }
variable "image_uri" { type = string }
variable "queue_url" { type = string }
variable "queue_arn" { type = string }

# The API starts the worker on enqueue — see adapters/queue_sqs.py for why the wake-up
# lives here rather than in an event-source mapping or a poller.
variable "batch_job_queue" {
  type    = string
  default = ""
}

variable "batch_job_definition" {
  type    = string
  default = ""
}
variable "ssm_path" { type = string }
variable "b2_bucket" { type = string }

# Not a secret, so it is declared config rather than an SSM parameter. It is also not
# optional: genblaze-s3 defaults to guessing us-west-004, and a bucket in a different
# B2 region answers HeadBucket with a bare 403 that reads exactly like a bad key.
variable "b2_region" {
  type    = string
  default = ""
}
variable "tenant_id" { type = string }

# The backend axes, surfaced as variables rather than hard-coded.
#
# They default to the production trio. They exist as knobs because a deployment can be
# legitimately live before its credentials are: with B2 keys still unset, `b2` makes the
# app refuse to start (by design — bootstrap.py will not hand a placeholder to a
# provider), so there would be no console at all. Setting them to local/mock yields a
# working, honestly-labelled console in the meantime — every page banners exactly which
# backends are in play, so this cannot be mistaken for the real thing.
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

# Auth defaults to `none` — the same shape this deployment has always had, so applying
# this change alone does not lock anyone out of a running console. Turning it on is
# deliberate: set `auth_backend = "otp"`, populate /resend/api-key and SESSION_SECRET in
# SSM, and list the people who may sign in. Doing it in the other order would put a
# login page in front of a console whose signing key is a per-process random value.
variable "auth_backend" {
  type    = string
  default = "none"
}

variable "mail_backend" {
  type    = string
  default = "console"
}

variable "mail_from" {
  type    = string
  default = "rtp@mattrickslauer.com"
}

variable "mail_from_name" {
  type    = string
  default = "Respect the Funk"
}

# The Resend API key is shared across projects, so it lives outside this env's SSM path.
# bootstrap.load_secrets reads it by name at cold start; "" turns that off.
variable "resend_key_parameter" {
  type    = string
  default = "/resend/api-key"
}

variable "gcp_project" {
  type        = string
  default     = ""
  description = "Vertex project for the Google video/image fallback. Empty disables it."
}

variable "gcp_location" {
  type        = string
  default     = "us-central1"
  description = "Vertex region for the Google fallback."
}

variable "require_auth" {
  type        = bool
  default     = false
  description = <<-EOT
    Refuse to start rather than serve an unauthenticated console that believes it is
    authenticated. Two checks, both in deps.py: `auth_backend = "none"` with this set is
    fatal, and so is an empty RK_SESSION_SECRET — which would otherwise fall back to a
    per-process random key, signing sessions that no other Lambda instance can verify.

    The consequence to know before setting it: if SESSION_SECRET is not in SSM, the
    function does not boot. That is deliberate — a login that silently logs everyone out
    on every cold start is harder to diagnose than one that never came up.
  EOT
}

variable "allowed_emails" {
  type        = list(string)
  default     = []
  description = "Addresses permitted to sign in. Empty means nobody can — see deps.describe()."
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "memory_mb" {
  type = number
  # Lambda scales CPU with memory. 1024 MB is roughly one vCPU — enough that template
  # rendering and JSON work are not artificially slow, while staying inside the free
  # tier's GB-seconds for a demo's traffic.
  default = 1024
}

variable "timeout_seconds" {
  type = number
  # 30s, not 900. Nothing slow runs here — that is the entire reason the generator is
  # Batch. A handler approaching this timeout is a bug, and a short ceiling surfaces
  # it instead of hiding it behind a 15-minute wait.
  default = 30
}

variable "log_retention_days" {
  type    = number
  default = 14 # infra/README: logs at 14-day retention, ~cents
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------- iam
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name_prefix}-api"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "app" {
  # Enqueue only. The API can start a kit; it cannot consume the queue, because
  # consuming is the worker's job and a web tier that could would be one bug away
  # from running a 15-minute generation inside a 30-second handler.
  statement {
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
    resources = [var.queue_arn]
  }

  # Start the generator, and nothing else. Submitting a job is not the same authority
  # as managing Batch: it cannot create or alter a job definition, so it cannot change
  # what the worker runs.
  dynamic "statement" {
    for_each = var.batch_job_queue == "" ? [] : [1]
    content {
      actions   = ["batch:SubmitJob"]
      resources = [var.batch_job_queue, var.batch_job_definition]
    }
  }

  # Read its own secrets, and nothing else's — scoped to this env's path.
  #
  # Both ARNs are required, and the missing one is not obvious: `GetParameter` acts on
  # an individual parameter (the `/*` form), but `GetParametersByPath` acts on the
  # *path node itself* (the bare form). Granting only `/*` yields an AccessDenied that
  # names a resource — `parameter/remixkit/prod` — which does not visibly appear in the
  # policy, and the Lambda dies during init rather than at first use.
  statement {
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_path}",
      "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_path}/*",
    ]
  }

  # The one secret outside that path: the shared Resend key, read by name.
  dynamic "statement" {
    for_each = var.resend_key_parameter == "" ? [] : [1]
    content {
      actions   = ["ssm:GetParameter"]
      resources = ["arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.resend_key_parameter}"]
    }
  }

  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "${var.name_prefix}-api"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.app.json
}

# ---------------------------------------------------------------- function
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name_prefix}-api"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "this" {
  function_name = "${var.name_prefix}-api"
  role          = aws_iam_role.this.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = var.memory_mb
  timeout       = var.timeout_seconds

  # RK_ALLOWED_EMAILS is not a secret, so it lives here rather than in SSM: an operator
  # adding a colleague should be editing a tfvars line under review, not running
  # put-parameter by hand. RK_RESEND_API_KEY and RK_SESSION_SECRET *are* secrets, and
  # arrive from SSM via bootstrap.load_secrets — they are deliberately absent below.
  environment {
    variables = {
      RK_ENV               = var.env
      RK_STORAGE_BACKEND   = var.storage_backend
      RK_GENERATOR_BACKEND = var.generator_backend
      RK_QUEUE_BACKEND     = var.queue_backend
      RK_AUTH_BACKEND      = var.auth_backend
      RK_REQUIRE_AUTH      = tostring(var.require_auth)
      RK_MAIL_BACKEND      = var.mail_backend
      RK_MAIL_FROM         = var.mail_from
      RK_MAIL_FROM_NAME    = var.mail_from_name
      # The parameter's *name*, not its value — the key itself never enters state.
      RK_RESEND_KEY_PARAMETER = var.resend_key_parameter
      RK_ALLOWED_EMAILS       = join(",", var.allowed_emails)
      # Vertex project/location for the Google (Veo/Imagen) provider fallback. These
      # are not secrets. NOTE: they are not sufficient on their own — Lambda has no
      # Application Default Credentials, so the Google path stays inert here until a
      # service-account key is added. See app/README "Which provider actually runs".
      GCP_PROJECT             = var.gcp_project
      GCP_LOCATION            = var.gcp_location
      RK_SQS_QUEUE_URL        = var.queue_url
      RK_B2_BUCKET            = var.b2_bucket
      RK_B2_REGION            = var.b2_region
      RK_DEFAULT_TENANT_ID    = var.tenant_id
      RK_AWS_REGION           = data.aws_region.current.name
      RK_SSM_PATH             = var.ssm_path
      RK_BATCH_JOB_QUEUE      = var.batch_job_queue
      RK_BATCH_JOB_DEFINITION = var.batch_job_definition
      AWS_LWA_INVOKE_MODE     = "buffered"
    }
  }

  depends_on = [aws_cloudwatch_log_group.this]
  tags       = var.tags
}

resource "aws_lambda_function_url" "this" {
  function_name = aws_lambda_function.this.function_name
  # NONE means "no *AWS* gate on this URL" — it does not mean the console is open. The
  # application is the gate: with RK_AUTH_BACKEND=otp every route raises AuthError
  # without a session, and this URL serves a login page rather than a roster.
  #
  # It stays NONE because AWS_IAM would require every browser request to be SigV4-signed,
  # which a browser cannot do. Moving the gate out here at all means an authorizer, and
  # that is only worth building if the app's own session stops being sufficient.
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["*"]
  }
}

# A Function URL with auth type NONE still needs a resource-based policy granting
# public invoke. The console adds this for you; the API and Terraform do not — so
# without it the URL exists, resolves, and returns 403 to everyone. This is the
# difference between "deployed" and "reachable".
resource "aws_lambda_permission" "public_url" {
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

output "function_url" { value = aws_lambda_function_url.this.function_url }
output "function_name" { value = aws_lambda_function.this.function_name }
output "role_arn" { value = aws_iam_role.this.arn }

output "function_arn" { value = aws_lambda_function.this.arn }
