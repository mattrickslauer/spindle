# SSM Parameter Store — B2 and provider credentials.
#
# infra/README ④, and the choice is load-bearing for the headline claim: Secrets
# Manager is $0.40 per secret per month, which is a real fixed floor against "under
# $1/month idle". SSM Standard is free. With five or six secrets that is the
# difference between ~$2.40/month of pure idle and $0.00.
#
# Values are NOT set here. Terraform state would record them in plaintext, so the
# parameters are created empty and populated out of band:
#
#   aws ssm put-parameter --name /remixkit/prod/B2_APP_KEY --type SecureString \
#     --value "…" --overwrite

variable "name_prefix" { type = string }
variable "env" { type = string }
variable "tags" {
  type    = map(string)
  default = {}
}

variable "secret_names" {
  type = list(string)
  default = [
    "B2_KEY_ID",
    "B2_APP_KEY",
    "GMI_API_KEY",
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    # Mail is Resend, whose key is shared at /resend/api-key — see modules/api.
    # Signs console sessions and hashes OTP codes. Rotating it signs everyone out —
    # which is the only revocation a stateless session has, so it is worth knowing.
    # Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'
    "SESSION_SECRET",
  ]
  description = "Parameters to create. Values are populated out of band."
}

locals {
  prefix = "/${var.name_prefix}/${var.env}"
}

resource "aws_ssm_parameter" "secret" {
  for_each = toset(var.secret_names)

  name  = "${local.prefix}/${each.value}"
  type  = "SecureString"
  value = "PLACEHOLDER — set with `aws ssm put-parameter --overwrite`"
  tags  = var.tags

  lifecycle {
    # Terraform creates the parameter and then never touches its value again.
    # Without this, every apply would reset real credentials to the placeholder.
    ignore_changes = [value]
  }
}

output "parameter_path" { value = local.prefix }
output "parameter_arns" { value = [for p in aws_ssm_parameter.secret : p.arn] }
output "parameter_names" { value = [for p in aws_ssm_parameter.secret : p.name] }
